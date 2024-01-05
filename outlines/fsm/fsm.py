"""
Copyright 2023- The Outlines developers

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from typing import TYPE_CHECKING, List, NewType, Optional, Protocol

import interegular
from lark import Lark

# from outlines.fsm.parsing import PartialLark

from outlines.fsm.regex_pure_numpy import create_fsm_index_tokenizer, make_deterministic_fsm

if TYPE_CHECKING:
    from outlines.models.tokenizer import Tokenizer

FSMState = NewType("FSMState", int)


class FSM(Protocol):
    def allowed_token_ids(self, state: FSMState, idx: int = 0) -> List[int]:
        ...

    def next_state(self, state: FSMState, token_id: int, idx: int = 0) -> FSMState:
        ...

    def is_final_state(self, state: FSMState, idx: int = 0) -> bool:
        ...

    def reset(self) -> None:
        ...


class StopAtTokenFSM(FSM):
    """FSM to generate text until a specified token id is generated or
    a specified number of tokens has been generated.

    Text is usually produced until the EOS token is generated by the
    model.

    """

    def __init__(
        self,
        tokenizer: "Tokenizer",
        stop_token_id: int,
        max_tokens: Optional[int] = None,
    ):
        self.stop_token_id = stop_token_id
        self.max_tokens = max_tokens
        self.num_tokens_generated = 0
        self.vocabulary = tokenizer.vocabulary.values()
        self.final_states = {1}

    def allowed_token_ids(self, state: FSMState, idx: int = 0) -> List[int]:
        """Generate a list of allowed tokens for the next step.

        When in the initial state we allow every token to be generated.
        In the final state the only allowed token is `stop_token_id`.

        Parameters
        ----------
        state
            The current state of the FSM.
        idx
            The index of the current input in the batch.

        Returns
        -------
        A list that contains the tokens to mask.

        """
        if state == 0:
            return list(self.vocabulary)
        else:
            return [self.stop_token_id]

    def next_state(self, state: FSMState, token_id: int, idx: int = 0) -> FSMState:
        """Update the state of the FSM.

        The FSM stays in the initial state `0` unless the specified stop token
        has been generated or the maximum number of tokens has been reached. In
        which case the FSM moves to the final state `1`.

        Parameters
        ----------
        state
            The current state of the FSM.
        token_id
            The id of the token that was just generated.
        idx
            The index of the current input in the batch.

        Returns
        -------
        The new state of the FSM.

        """
        if idx == 0:
            self.num_tokens_generated += 1

        if self.max_tokens is not None:
            if self.num_tokens_generated >= self.max_tokens:
                return FSMState(1)

        if token_id == self.stop_token_id:
            return FSMState(1)

        return FSMState(0)

    def is_final_state(self, state: FSMState, idx: int = 0) -> bool:
        """Determine whether the current state of the FSM is a final state."""
        return state in self.final_states

    def reset(self) -> None:
        """Reset the FSM to its initial state. Here this only resets the token counter."""
        self.num_tokens_generated = 0


class RegexFSM(FSM):
    """FSM to generate text that is in the language of a regular expression."""

    def __init__(
        self,
        regex_string: str,
        tokenizer: "Tokenizer",
        max_tokens: Optional[int] = None,
    ):
        regex_pattern = interegular.parse_pattern(regex_string)
        regex_fsm, _ = make_deterministic_fsm(regex_pattern.to_fsm().reduce())
        (
            self.states_to_token_maps,
            self.empty_token_ids,
        ) = create_fsm_index_tokenizer(regex_fsm, tokenizer)

        # We make sure that it is possible to generate strings in the language
        # of the regular expression with the tokens present in the model's
        # vocabulary.
        if not any(
            regex_fsm.finals.intersection(v.values())
            for v in self.states_to_token_maps.values()
        ):
            raise ValueError(
                "The vocabulary does not allow us to build a sequence that matches the input regex"
            )

        self.final_states = regex_fsm.finals | {
            -1
        }  # Include the EOS token in final states
        self.max_tokens = max_tokens
        self.num_tokens_generated = 0
        self.vocabulary = tokenizer.vocabulary.values()
        self.end_token_id = tokenizer.eos_token_id

    def allowed_token_ids(self, state: FSMState, idx: int = 0) -> List[int]:
        """Generate a list of allowed tokens for the next step.

        The initialization of the FSM builds an index which maps FSM states to a
        map from authorized tokens to the state in which the FSM needs to move
        if said token is generated. Therefore the authorized tokens at the
        current state are the keys of the map returned by the value of the index
        for current state.

        If the current state is not contained in the end this means that we are
        in a final state of the FSM. We only authorize EOS tokens in the final
        state.

        Parameters
        ----------
        state
            The current state of the FSM.
        idx
            The index of the current input in the batch.

        Returns
        -------
        A list that contains the tokens to mask.

        """
        next_tokens_to_end_states = self.states_to_token_maps.get(state)

        if next_tokens_to_end_states is None:
            return [self.end_token_id]
        else:
            return list(next_tokens_to_end_states.keys())

    def next_state(self, state: FSMState, token_id: int, idx: int = 0) -> FSMState:
        """Update the state of the FSM.

        We use the index to determine to which state the FSM should transition
        given the token that was just generated.

        Parameters
        ----------
        state
            The current state of the FSM.
        token_id
            The id of the token that was just generated.
        idx
            The index of the current input in the batch.

        Returns
        -------
        The new state of the FSM.

        """
        if idx == 0:
            self.num_tokens_generated += 1

        if self.max_tokens is not None:
            if self.num_tokens_generated == self.max_tokens:
                return FSMState(-1)

        if token_id == self.end_token_id:
            return FSMState(-1)

        last_token_to_end_state = self.states_to_token_maps[state]
        next_state = last_token_to_end_state.get(token_id)
        if next_state is None:
            next_state = -1

        return FSMState(next_state)

    def is_final_state(self, state: FSMState, idx: int = 0) -> bool:
        """Determine whether the current state of the FSM is a final state."""
        return state in self.final_states

    def reset(self) -> None:
        """Reset the FSM to its initial state. Here this only resets the token counter."""
        self.num_tokens_generated = 0


class CFGFSM(FSM):
    """FSM to generate text that is in the language of a context-free grammar."""

    def __init__(
        self,
        cfg_string: str,
        tokenizer: "Tokenizer",
        max_tokens: Optional[int] = None,
    ):
        # self.parser = PartialLark(cfg_string, parser="lalr")
        self.parser = Lark(
            cfg_string,
            parser="lalr",
            lexer="contextual",
            propagate_positions=False,
            maybe_placeholders=False,
            regex=True,
        )
        self.terminal_regexps = dict()
        for terminal in self.parser.terminals:
            if terminal.pattern is not None:
                self.terminal_regexps[terminal.name] = terminal.pattern.to_regexp()
        self.terminal_regexps["$END"] = tokenizer.eos_token

        self.tokenizer = tokenizer
        self.max_tokens = max_tokens
        self.num_tokens_generated = 0
        self.generations: List[str] = []
        self.regex_fsms: List[RegexFSM] = []
        self.reset_state: List[bool] = []
        self.allow_eos: List[bool] = []
        self.done: List[bool] = []

    def _set_next_regex_fsm(self, idx: int = 0) -> None:
        """Use the CFG incremental parser to set the next regex FSM.

        Check what the CFG incremental parser proposes next.
        If the only proposal is the EOS token,
            we set the state to done and return.
        If there are other proposals,
            we set a new regex FSM and return.

        """
        interactive = self.parser.parse_interactive(self.generations[idx])
        interactive.exhaust_lexer()
        options = {self.terminal_regexps[x] for x in interactive.accepts()}

        if self.terminal_regexps["$END"] in options:
            options.remove(self.terminal_regexps["$END"])
            if len(options) == 0:
                self.done[idx] = True
                return
            self.allow_eos[idx] = True
            options.add("")
            assert len(options) > 1

        regex_string = r"(" + r"|".join([r"(" + x + r")" for x in options]) + r")"
        args = (
            regex_string,
            self.tokenizer,
            self.max_tokens - self.num_tokens_generated if self.max_tokens else None,
        )
        if len(self.regex_fsms) <= idx:
            self.regex_fsms.append(RegexFSM(*args))
        else:
            self.regex_fsms[idx] = RegexFSM(*args)
        self.reset_state[idx] = True

    def allowed_token_ids(self, state: FSMState, idx: int = 0) -> List[int]:
        """Generate a list of allowed tokens for the next step.

        Upon initialization, the CFG incremental parser is used to determine the first regex.

        This regex is used for proposals until either:
        - the regex is exhausted, and its only remaining option is the EOS token,
            in which case we always transition to the next regex
        - the regex can be exhausted, but the EOS token is not the only remaining option,
            in which case we transition to the next regex with probability P (TODO)
            or remove the possibility of generating the EOS token and continue with the current regex

        The CFG incremental parser is allowed to propose the EOS token from any final state,
        and once it is generated, the FSM will continue to always generate the EOS token.

        Parameters
        ----------
        state
            The current state of the FSM.
        idx
            The index of the current input in the batch.

        Returns
        -------
        A list that contains the tokens to mask.

        """
        if len(self.generations) <= idx:
            self.generations.append("")
            self.reset_state.append(False)
            self.allow_eos.append(False)
            self.done.append(False)

        if len(self.regex_fsms) > idx:
            proposal = self.regex_fsms[idx].allowed_token_ids(state)
            if self.tokenizer.eos_token_id not in proposal:
                return proposal
            if set(proposal) != {self.tokenizer.eos_token_id}:
                if False:  # TODO: THIS NEEDS TO BE SAMPLED
                    proposal = [x for x in proposal if x != self.tokenizer.eos_token_id]
                    return proposal

        self._set_next_regex_fsm(idx)

        if self.done[idx]:
            return [self.tokenizer.eos_token_id]

        if self.reset_state[idx]:
            state = FSMState(0)

        proposal = self.regex_fsms[idx].allowed_token_ids(state)
        if self.allow_eos[idx]:
            self.allow_eos[idx] = False
        else:
            proposal = [x for x in proposal if x != self.tokenizer.eos_token_id]
            assert len(proposal) > 0
        return proposal

    def next_state(self, state: FSMState, token_id: int, idx: int = 0) -> FSMState:
        """Update the state of the FSM.

        Transitions the underlying regex FSM to its next state.
        If at max tokens or EOS token, transition permanently to the final state.
        Update stored partial generations for subsequent incremental parsing.

        Parameters
        ----------
        state
            The current state of the FSM.
        token_id
            The id of the token that was just generated.
        idx
            The index of the current input in the batch.

        Returns
        -------
        The new state of the FSM.
        """
        if idx == 0:
            self.num_tokens_generated += 1
        if self.max_tokens is not None:
            if self.num_tokens_generated >= self.max_tokens:
                self.done[idx] = True
                return FSMState(-1)
        if token_id == self.tokenizer.eos_token_id:
            self.done[idx] = True
            return FSMState(-1)
        if self.reset_state[idx]:
            self.reset_state[idx] = False
            state = FSMState(0)

        self.generations[idx] += self.tokenizer.decode([token_id])[0]

        return self.regex_fsms[idx].next_state(state, token_id, idx)

    def is_final_state(self, state: FSMState, idx: int = 0) -> bool:
        """Return whether the current state of the FSM is a final state."""
        return self.done[idx]

    def reset(self) -> None:
        """Reset the FSM to its initial state, so it can be called on a fresh batch on inputs."""
        self.num_tokens_generated = 0
        self.generations = []
        self.regex_fsms = []
        self.reset_state = []
        self.done = []
