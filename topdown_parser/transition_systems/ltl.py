from dataclasses import dataclass
from typing import List, Iterable, Optional, Tuple, Dict, Any, Set

import torch

from topdown_parser.am_algebra.tree import Tree
from topdown_parser.dataset_readers.AdditionalLexicon import AdditionalLexicon
from topdown_parser.dataset_readers.amconll_tools import AMSentence
from topdown_parser.nn.utils import get_device_id
from topdown_parser.transition_systems.transition_system import TransitionSystem, Decision, get_parent, get_siblings
from topdown_parser.transition_systems.utils import scores_to_selection


@TransitionSystem.register("dfs-children-first")
class DFSChildrenFirst(TransitionSystem):
    """
    DFS where when a node is visited for the second time, all its children are visited once.
    Afterwards, the first child is visited for the second time. Then the second child etc.
    """

    def __init__(self, children_order: str, pop_with_0: bool,
                 additional_lexicon: AdditionalLexicon,
                 reverse_push_actions: bool = False):
        """
        Select children_order : "LR" (left to right) or "IO" (inside-out, recommended by Ma et al.)
        reverse_push_actions means that the order of push actions is the opposite order in which the children of
        the node are recursively visited.
        """
        super().__init__(additional_lexicon)
        self.pop_with_0 = pop_with_0
        self.reverse_push_actions = reverse_push_actions
        assert children_order in ["LR", "IO", "RL"], "unknown children order"

        self.children_order = children_order

    def predict_supertag_from_tos(self) -> bool:
        return True

    def _construct_seq(self, tree: Tree) -> List[Decision]:
        own_position = tree.node[0]
        push_actions = []
        recursive_actions = []

        if self.children_order == "LR":
            children = tree.children
        elif self.children_order == "RL":
            children = reversed(tree.children)
        elif self.children_order == "IO":
            left_part = []
            right_part = []
            for child in tree.children:
                if child.node[0] < own_position:
                    left_part.append(child)
                else:
                    right_part.append(child)
            children = list(reversed(left_part)) + right_part
        else:
            raise ValueError("Unknown children order: "+self.children_order)

        for child in children:
            if child.node[1].label == "IGNORE":
                continue

            push_actions.append(Decision(child.node[0], child.node[1].label, ("", ""), ""))
            recursive_actions.extend(self._construct_seq(child))

        if self.pop_with_0:
            relevant_position = 0
        else:
            relevant_position = own_position

        if self.reverse_push_actions:
            push_actions = list(reversed(push_actions))

        return push_actions + [Decision(relevant_position, "", (tree.node[1].fragment, tree.node[1].typ), tree.node[1].lexlabel)] + recursive_actions

    def get_order(self, sentence: AMSentence) -> Iterable[Decision]:
        t = Tree.from_am_sentence(sentence)
        r = [Decision(t.node[0], t.node[1].label, ("", ""), "")] + self._construct_seq(t)
        return r

    def check_correct(self, gold_sentence : AMSentence, predicted : AMSentence) -> bool:
        return all(x.head == y.head for x, y in zip(gold_sentence, predicted)) and \
               all(x.label == y.label for x, y in zip(gold_sentence, predicted)) and \
               all(x.fragment == y.fragment and x.typ == y.typ for x, y in zip(gold_sentence, predicted))

    def get_additional_choices(self, decision : Decision) -> Dict[str, List[str]]:
        """
        Turn a decision into a dictionary of additional choices (beyond the node that is selected)
        :param decision:
        :return:
        """
        r = dict()
        if decision.label != "":
            r["selected_edge_labels"] = [decision.label]
        if decision.supertag != ("",""):
            r["selected_constants"] = ["--TYPE--".join(decision.supertag)]
        if decision.lexlabel != "":
            r["selected_lex_labels"] = [decision.lexlabel]

        return r

    def reset_parses(self, sentences: List[AMSentence], input_seq_len: int) -> None:
        self.input_seq_len = input_seq_len
        self.batch_size = len(sentences)
        self.stack = [[0] for _ in range(self.batch_size)]  # 1-based
        self.sub_stack = [[] for _ in range(self.batch_size)]  # 1-based
        self.seen = [{0} for _ in range(self.batch_size)]
        self.heads = [[0 for _ in range(len(sentence.words))] for sentence in sentences]
        self.children = [{i: [] for i in range(len(sentence.words) + 1)} for sentence in sentences]  # 1-based
        self.labels = [["IGNORE" for _ in range(len(sentence.words))] for sentence in sentences]
        self.lex_labels = [["_" for _ in range(len(sentence.words))] for sentence in sentences]
        self.supertags = [["_--TYPE--_" for _ in range(len(sentence.words))] for sentence in sentences]
        self.sentences = sentences

    def get_additional_choices(self, decision : Decision) -> Dict[str, List[str]]:
        """
        Turn a decision into a dictionary of additional choices (beyond the node that is selected)
        :param decision:
        :return:
        """
        return {"selected_edge_labels" : [decision.label], "selected_constants" : ["--TYPE--".join(decision.supertag)]}

    def step(self, selected_nodes: torch.Tensor, additional_scores : Dict[str, torch.Tensor]) -> Tuple[torch.Tensor, torch.Tensor]:
        device = get_device_id(selected_nodes)
        assert selected_nodes.shape == (self.batch_size,)

        selected_nodes = selected_nodes.cpu().numpy()
        selected_labels = scores_to_selection(additional_scores, self.additional_lexicon, "edge_labels")
        selected_supertags = scores_to_selection(additional_scores, self.additional_lexicon, "constants")
        selected_lex_labels = scores_to_selection(additional_scores, self.additional_lexicon, "lex_labels")

        r = []
        next_choices = []
        for i in range(self.batch_size):
            if self.stack[i]:
                selected_node_in_batch_element = int(selected_nodes[i])

                if (selected_node_in_batch_element in self.seen[i] and not self.pop_with_0) or \
                    (selected_node_in_batch_element == 0 and self.pop_with_0):
                    popped = self.stack[i].pop()

                    if not self.pop_with_0:
                        assert popped == selected_nodes[i]

                    if selected_supertags is not None:
                        self.supertags[i][popped-1] = selected_supertags[i]

                    if selected_lex_labels is not None:
                        self.lex_labels[i][popped-1] = selected_lex_labels[i]

                    if self.reverse_push_actions:
                        self.stack[i].extend(self.sub_stack[i])
                    else:
                        self.stack[i].extend(reversed(self.sub_stack[i]))
                    self.sub_stack[i] = []
                else:
                    self.heads[i][selected_node_in_batch_element - 1] = self.stack[i][-1]

                    self.children[i][self.stack[i][-1]].append(selected_node_in_batch_element)  # 1-based

                    self.labels[i][selected_node_in_batch_element - 1] = selected_labels[i]

                    # push onto stack
                    self.sub_stack[i].append(selected_node_in_batch_element)

                self.seen[i].add(selected_node_in_batch_element)
                choices = torch.zeros(self.input_seq_len)

                if not self.stack[i]:
                    r.append(0)
                else:
                    r.append(self.stack[i][-1])
                    # we can close the current node:
                    if self.pop_with_0:
                        choices[0] = 1
                    else:
                        choices[self.stack[i][-1]] = 1

                for child in range(1, len(self.sentences[i]) + 1):  # or we can choose a node that has not been used yet
                    if child not in self.seen[i]:
                        choices[child] = 1
                next_choices.append(choices)
            else:
                r.append(0)
                next_choices.append(torch.zeros(self.input_seq_len))

        return torch.tensor(r, device=device), torch.stack(next_choices, dim=0)

    def retrieve_parses(self) -> List[AMSentence]:
        assert all(stack == [] for stack in self.stack)  # all parses complete

        sentences = [sentence.set_heads(self.heads[i]) for i, sentence in enumerate(self.sentences)]
        sentences = [sentence.set_labels(self.labels[i]) for i, sentence in enumerate(sentences)]
        sentences = [sentence.set_supertags(self.supertags[i]) for i, sentence in enumerate(sentences)]
        sentences = [sentence.set_lexlabels(self.lex_labels[i]) for i, sentence in enumerate(sentences)]

        self.sentences = None
        self.stack = None
        self.sub_stack = None
        self.seen = None
        self.heads = None
        self.batch_size = None
        self.supertags = None
        self.lex_labels = None

        return sentences

    def gather_context(self, active_nodes: torch.Tensor) -> Dict[str, torch.Tensor]:
        """

        :param active_nodes: tensor of shape (batch_size,) with nodes that are currently on top of the stack.
        :return:
        """
        return super()._gather_context(active_nodes, self.batch_size, self.heads, self.children, self.labels)