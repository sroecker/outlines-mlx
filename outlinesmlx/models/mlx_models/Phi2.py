import argparse
from typing import Optional
from dataclasses import dataclass
from mlx.utils import tree_flatten, tree_map, tree_unflatten
from transformers import AutoTokenizer

import mlx.core as mx
import mlx.nn as nn
import math
from pathlib import Path
import os
from transformers import AutoModelForCausalLM
import numpy as np 
import json
import copy

@dataclass
class ModelArgs:
    max_sequence_length: int = 2048
    num_vocab: int = 51200
    model_dim: int = 2560
    num_heads: int = 32
    num_layers: int = 32
    rotary_dim: int = 32


class LayerNorm(nn.LayerNorm):
    def __call__(self, x: mx.array) -> mx.array:
        return super().__call__(x.astype(mx.float32)).astype(x.dtype)


class RoPEAttention(nn.Module):
    def __init__(self, dims: int, num_heads: int, rotary_dim: int):
        super().__init__()

        self.num_heads = num_heads

        self.rope = nn.RoPE(rotary_dim, traditional=False)
        self.Wqkv = nn.Linear(dims, 3 * dims)
        self.out_proj = nn.Linear(dims, dims)

    def __call__(self, x, mask=None, cache=None):
        qkv = self.Wqkv(x)
        queries, keys, values = mx.split(qkv, 3, axis=-1)

        # Extract some shapes
        num_heads = self.num_heads
        B, L, D = queries.shape

        # Prepare the queries, keys and values for the attention computation
        queries = queries.reshape(B, L, num_heads, -1).transpose(0, 2, 1, 3)
        keys = keys.reshape(B, L, num_heads, -1).transpose(0, 2, 1, 3)
        values = values.reshape(B, L, num_heads, -1).transpose(0, 2, 1, 3)

        # Add RoPE to the queries and keys and combine them with the cache
        if cache is not None:
            key_cache, value_cache = cache
            queries = self.rope(queries, offset=key_cache.shape[2])
            keys = self.rope(keys, offset=key_cache.shape[2])
            keys = mx.concatenate([key_cache, keys], axis=2)
            values = mx.concatenate([value_cache, values], axis=2)
        else:
            queries = self.rope(queries)
            keys = self.rope(keys)

        queries = queries.astype(mx.float32)
        keys = keys.astype(mx.float32)

        # Finally perform the attention computation
        scale = math.sqrt(1 / queries.shape[-1])
        scores = (queries * scale) @ keys.transpose(0, 1, 3, 2)
        if mask is not None:
            scores = scores + mask

        scores = mx.softmax(scores, axis=-1).astype(values.dtype)
        values_hat = (scores @ values).transpose(0, 2, 1, 3).reshape(B, L, -1)

        return self.out_proj(values_hat), (keys, values)


class ParallelBlock(nn.Module):
    def __init__(self, config: ModelArgs):
        super().__init__()
        dims = config.model_dim
        mlp_dims = dims * 4
        self.mixer = RoPEAttention(dims, config.num_heads, config.rotary_dim)
        self.ln = LayerNorm(dims)
        self.fc1 = nn.Linear(dims, mlp_dims)
        self.fc2 = nn.Linear(mlp_dims, dims)
        self.act = nn.GELU(approx="precise")

    def __call__(self, x, mask, cache):
        h = self.ln(x)
        attn_h, cache = self.mixer(h, mask, cache)
        ff_h = self.fc2(self.act(self.fc1(h)))
        return attn_h + ff_h + x, cache


class TransformerDecoder(nn.Module):
    def __init__(self, config: ModelArgs):
        super().__init__()
        self.h = [ParallelBlock(config) for i in range(config.num_layers)]

    def __call__(self, x, mask, cache):
        if cache is None:
            cache = [None] * len(self.h)

        for e, layer in enumerate(self.h):
            x, cache[e] = layer(x, mask, cache[e])
        return x, cache


class OutputHead(nn.Module):
    def __init__(self, config: ModelArgs) -> None:
        self.ln = LayerNorm(config.model_dim)
        self.linear = nn.Linear(config.model_dim, config.num_vocab)

    def __call__(self, inputs):
        return self.linear(self.ln(inputs))


class Phi2(nn.Module):
    def __init__(self, config: ModelArgs):
        self.wte = nn.Embedding(config.num_vocab, config.model_dim)
        self.transformer = TransformerDecoder(config)
        self.lm_head = OutputHead(config)

    def __call__(
        self,
        inputs: mx.array,
        mask: mx.array = None,
        cache: mx.array = None,
    ) -> tuple[mx.array, mx.array]:
        

        x = self.wte(inputs)

        mask = None
        if x.shape[1] > 1:
            mask = nn.MultiHeadAttention.create_additive_causal_mask(x.shape[1])
            mask = mask.astype(x.dtype)

        y, cache = self.transformer(x, mask, cache)
        
        return self.lm_head(y), cache
    

def replace_key(key: str) -> str:
    if "wte.weight" in key:
        key = "wte.weight"

    if ".mlp" in key:
        key = key.replace(".mlp", "")
    return key

def start_conversion(model_name:str):

    print("Model not found. Converting to mlx format...")

    model = AutoModelForCausalLM.from_pretrained(
        model_name, torch_dtype="auto", trust_remote_code=True
    )
    state_dict = model.state_dict()
    weights = {replace_key(k): v.numpy() for k, v in state_dict.items()}
    params = {}

    return(weights,params)

def quantize(weights, config, args):
    quantized_config = copy.deepcopy(config)

    # Load the model:
    model = Phi2(ModelArgs())
    weights = tree_map(mx.array, weights)
    model.update(tree_unflatten(list(weights.items())))

    # Quantize the model:
    nn.QuantizedLinear.quantize_module(model, args.q_group_size, args.q_bits)

    # Update the config:
    quantized_config["quantization"] = {
        "group_size": args.q_group_size,
        "bits": args.q_bits,
    }
    quantized_weights = dict(tree_flatten(model.parameters()))

    return quantized_weights, quantized_config

class AttrDict:
    def __init__(self, d):
        for key, value in d.items():
            setattr(self, key, value)

def load_model(model_name:str,**model_kwargs):

    args = AttrDict(model_kwargs)

    mlx_path = Path("/tmp/mlx_models/"+model_name)
    mlx_path.mkdir(parents=True, exist_ok=True)

    #Check if it already exists
    if ((not (os.path.exists(str(mlx_path / "weights.npz")) and os.path.exists(str(mlx_path / "config.json")))) or args.force_conversion):
        weights,params = start_conversion(model_name)
        if args.quantize:
            print("[INFO] Quantizing")
            weights, params = quantize(weights, params, args)

        np.savez(str(mlx_path / "weights.npz"), **weights)
        
        with open(mlx_path / "config.json", "w") as fid:
            params["model_type"] = "phi2"
            json.dump(params, fid, indent=4)

    #Load the weigths and config file and create the model
    model = Phi2(ModelArgs())
    with open(mlx_path / "config.json", "r") as f:
        config = json.loads(f.read())
        config.pop("model_type", None)
        quantization = config.pop("quantization", None)
    weights = mx.load(str(mlx_path / "weights.npz"))
    weights = tree_unflatten(list(weights.items()))
    if quantization is not None:
        nn.QuantizedLinear.quantize_module(model, **quantization)
    model.update(weights)

    return model