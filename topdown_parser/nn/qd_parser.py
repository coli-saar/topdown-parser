import heapq
import socket
import time
from typing import Dict, List, Any, Optional, Tuple

import torch
import torch.nn.functional as F
from allennlp.common.checks import ConfigurationError
from allennlp.data import Vocabulary
from allennlp.models import Model
from allennlp.modules import TextFieldEmbedder, Embedding, Seq2SeqEncoder, InputVariationalDropout
from allennlp.nn.util import get_text_field_mask, get_final_encoder_states, get_range_vector, get_device_of, \
    batch_tensor_dicts
from torch.nn import Dropout, Dropout2d

from topdown_parser.dataset_readers.AdditionalLexicon import AdditionalLexicon, Lexicon
from topdown_parser.dataset_readers.amconll_tools import AMSentence
from topdown_parser.am_algebra.tools import is_welltyped, get_tree_type
from topdown_parser.losses.losses import EdgeExistenceLoss
from topdown_parser.nn import dm_edge_loss
from topdown_parser.nn.ContextProvider import ContextProvider
from topdown_parser.nn.DecoderCell import DecoderCell
from topdown_parser.nn.EdgeLabelModel import EdgeLabelModel
from topdown_parser.nn.EdgeModel import EdgeModel
from topdown_parser.nn.kg_edge_model import KGEdges
from topdown_parser.nn.parser import TopDownDependencyParser
from topdown_parser.nn.supertagger import Supertagger
from topdown_parser.nn.utils import get_device_id, batch_and_pad_tensor_dict, index_tensor_dict, expand_tensor_dict
from topdown_parser.transition_systems.dfs import DFS
from topdown_parser.transition_systems.ltf import LTF
from topdown_parser.transition_systems.parsing_state import undo_one_batching, undo_one_batching_eval, ParsingState
from topdown_parser.transition_systems.transition_system import TransitionSystem, Decision


def tensorize_app_edges(sents: List[AMSentence], lexicon: Lexicon) -> Tuple[torch.Tensor, torch.Tensor]:
    """

    :param sent:
    :return: tensor of shape (batch_size, input_seq_len+1, max number of children) with edge ids, mask
    with shape (batch_size, input_seq_len+1, max number of children)
    """
    batch_size = len(sents)
    input_seq_len = max(len(s) for s in sents)
    children_dicts = [s.children_dict() for s in sents]
    max_num_children = max(max(len(children) for children in d.values()) for d in children_dicts)
    ret = torch.zeros((batch_size, input_seq_len + 1, max_num_children), dtype=torch.long)
    mask = torch.zeros((batch_size, input_seq_len + 1, max_num_children))

    for b, (sent, children) in enumerate(zip(sents, children_dicts)):
        for i in range(1, input_seq_len + 1):
            if i in children:
                for child_i, child in enumerate(children[i]):
                    label = sent.words[child - 1].label
                    if label.startswith("APP_"):
                        ret[b, i, child_i] = lexicon.get_id(
                            label)  # store id of edge label. convert 1-based indexing to 0-based.
                        mask[b, i, child_i] = 1
    return ret, mask


@Model.register("qd-topdown")
class QdTopDownDependencyParser(TopDownDependencyParser):

    def __init__(self, vocab: Vocabulary,
                 text_field_embedder: TextFieldEmbedder,
                 encoder: Seq2SeqEncoder,
                 decoder: DecoderCell,
                 edge_model: EdgeModel,
                 edge_label_model: EdgeLabelModel,
                 edge_loss: EdgeExistenceLoss,
                 children_order: str,
                 pop_with_0: bool,
                 additional_lexicon: AdditionalLexicon,
                 label_embedding_dim: int,
                 tagger_encoder: Optional[Seq2SeqEncoder] = None,
                 kg_encoder: Optional[Seq2SeqEncoder] = None,
                 tagger_decoder: Optional[DecoderCell] = None,
                 supertagger: Optional[Supertagger] = None,
                 term_type_tagger: Optional[Supertagger] = None,
                 lex_label_tagger: Optional[Supertagger] = None,
                 context_provider: Optional[ContextProvider] = None,
                 tagger_context_provider: Optional[ContextProvider] = None,
                 pos_tag_embedding: Embedding = None,
                 lemma_embedding: Embedding = None,
                 ne_embedding: Embedding = None,
                 input_dropout: float = 0.0,
                 encoder_output_dropout: float = 0.0,
                 additional_edge_model: Optional[KGEdges] = None,
                 k_best : int = 1,
                 ):

        transition_system = LTF(children_order, pop_with_0, additional_lexicon)
        super().__init__(
            vocab, text_field_embedder, encoder, decoder, edge_model, edge_label_model,
            transition_system, edge_loss, tagger_encoder, kg_encoder,
            tagger_decoder, supertagger, term_type_tagger, lex_label_tagger, context_provider,
            tagger_context_provider, pos_tag_embedding, lemma_embedding, ne_embedding, input_dropout,
            encoder_output_dropout,
            additional_edge_model, k_best)

        self.unconstrained_transition_system = DFS(children_order, pop_with_0, additional_lexicon)

        self.children_label_embed = torch.nn.Embedding(additional_lexicon.vocab_size("edge_labels"),
                                                       label_embedding_dim)

    def tensorize_edges(self, sents: List[AMSentence], device: Optional[int]) -> torch.Tensor:
        children_label_tensor, children_label_mask = tensorize_app_edges(sents,
                                                                         self.transition_system.additional_lexicon.sublexica[
                                                                             "edge_labels"])
        children_label_tensor = children_label_tensor.to(device)  # shape (batch_size, isl, max num children)
        children_label_mask = children_label_mask.to(device)  # shape (batch_size, isl, max num children)

        children_label_rep = self.children_label_embed(children_label_tensor) * children_label_mask.unsqueeze(
            3)  # shape (batch_size, isl, max num children, embedding dim)
        children_label_rep = torch.sum(children_label_rep, dim=2)  # shape (batch_size, isl, embedding dim)
        return children_label_rep

    def forward(self, words: Dict[str, torch.Tensor],
            pos_tags: torch.LongTensor,
            lemmas: torch.LongTensor,
            ner_tags: torch.LongTensor,
            metadata: List[Dict[str, Any]],
            order_metadata: List[Dict[str, Any]],
            seq: Optional[torch.Tensor] = None,
            context : Optional[Dict[str, torch.Tensor]] = None,
            active_nodes : Optional[torch.Tensor] = None,
            labels: Optional[torch.Tensor] = None,
            label_mask: Optional[torch.Tensor] = None,
            lex_labels: Optional[torch.Tensor] = None,
            lex_label_mask: Optional[torch.Tensor] = None,
            supertags : Optional[torch.Tensor] = None,
            supertag_mask : Optional[torch.Tensor] = None,
            term_types : Optional[torch.Tensor] = None,
            term_type_mask : Optional[torch.Tensor] = None,
            heads : Optional[torch.Tensor] = None) -> Dict[str, torch.Tensor]:

        parsing_time_t0 = time.time()

        self.has_been_training_before = self.has_empty_tree_type or self.training
        batch_size, seq_len = pos_tags.shape
        # Encode the input:
        state = self.encode(words, pos_tags, lemmas, ner_tags)  # shape (batch_size, seq_len, encoder_dim)

        sentences = [ m["am_sentence"] for m in metadata]
        ret = {}
        if seq is not None and labels is not None and label_mask is not None and context is not None and self.has_been_training_before:
            children_label_rep = self.tensorize_edges(sentences, get_device_id(pos_tags))

            ret["loss"] = self.compute_loss(state, seq, active_nodes,
                                            labels, label_mask,
                                            supertags, supertag_mask,
                                            lex_labels, lex_label_mask,
                                            term_types, term_type_mask,
                                            heads, context, children_label_rep)

        if not self.training:
            sentences = [s.strip_annotation() for s in sentences]

            # Parse unconstrained
            if self.k_best == 1:
                predictions = self.parse_sentences(self.unconstrained_transition_system, state,
                                                   metadata[0]["formalism"], sentences, None)
            else:
                predictions = self.beam_search(self.unconstrained_transition_system, state, sentences, self.k_best, None)

            children_label_rep = self.tensorize_edges(predictions, get_device_id(pos_tags))

            # Parse with constraints, conditioning on predicted edge labels.
            if self.k_best == 1:
                predictions = self.parse_sentences(self.transition_system, state, metadata[0]["formalism"],
                                                   sentences, children_label_rep)
            else:
                predictions = self.beam_search(self.transition_system, state, sentences, self.k_best, children_label_rep)

            parsing_time_t1 = time.time()
            avg_parsing_time = (parsing_time_t1 - parsing_time_t0) / batch_size

            for pred in predictions:
                pred.attributes["normalized_parsing_time"] = str(avg_parsing_time)
                pred.attributes["batch_size"] = str(batch_size)
                pred.attributes["host"] = socket.gethostname()
                pred.attributes["beam_size"] = str(self.k_best)

            for p,g in zip(predictions, (m["am_sentence"] for m in metadata)):
                if p.get_root() == g.get_root():
                    self.root_correct += 1
                self.roots_total += 1

            #Compute some well-typedness statistics
            for p in predictions:
                ttyp = get_tree_type(p)
                if ttyp is not None:
                    self.well_typed += 1
                    self.has_empty_tree_type += int(ttyp.is_empty_type())
                else:
                    pass
                    # print("Not well-typed")
                    # print(p.get_tokens(False))
                self.sentences_parsed += 1

            ret["predictions"] = predictions

        return ret

    def annotate_loss(self, words: Dict[str, torch.Tensor],
                  pos_tags: torch.LongTensor,
                  lemmas: torch.LongTensor,
                  ner_tags: torch.LongTensor,
                  metadata: List[Dict[str, Any]],
                  order_metadata: List[Dict[str, Any]],
                  seq: Optional[torch.Tensor] = None,
                  context : Optional[Dict[str, torch.Tensor]] = None,
                  active_nodes : Optional[torch.Tensor] = None,
                  labels: Optional[torch.Tensor] = None,
                  label_mask: Optional[torch.Tensor] = None,
                  lex_labels: Optional[torch.Tensor] = None,
                  lex_label_mask: Optional[torch.Tensor] = None,
                  supertags : Optional[torch.Tensor] = None,
                  supertag_mask : Optional[torch.Tensor] = None,
                  term_types : Optional[torch.Tensor] = None,
                  term_type_mask : Optional[torch.Tensor] = None,
                  heads : Optional[torch.Tensor] = None) -> Dict[str, List[Any]]:
        """
        Same input as forward(), computes the loss of the individual sentences.
        """
        state = self.encode(words, pos_tags, lemmas, ner_tags)  # shape (batch_size, seq_len, encoder_dim)

        sentences : List[AMSentence] = [ m["am_sentence"] for m in metadata]
        # Parse unconstrained
        predictions = self.parse_sentences(self.unconstrained_transition_system, state,
                                           metadata[0]["formalism"], sentences, None)

        children_label_rep = self.tensorize_edges(predictions, get_device_id(pos_tags))


        #Parse with constraints, conditioning on predicted edge labels.
        sentence_loss = self.compute_sentence_loss(state, seq, active_nodes,
                                                   labels, label_mask,
                                                   supertags, supertag_mask,
                                                   lex_labels, lex_label_mask,
                                                   term_types, term_type_mask,
                                                   heads, context, children_label_rep)
        sentence_loss = sentence_loss.detach().cpu().numpy()
        batch_size = sentence_loss.shape[0]

        sentences : List[AMSentence] = [ m["am_sentence"] for m in metadata]
        for i in range(batch_size):
            sentences[i].attributes["loss"] = sentence_loss[i]
        return {"predictions": sentences}


    def compute_loss(self, state: Dict[str, torch.Tensor], seq: torch.Tensor, active_nodes : torch.Tensor,
                 labels: torch.Tensor, label_mask: torch.Tensor,
                 supertags : torch.Tensor, supertag_mask : torch.Tensor,
                 lex_labels : torch.Tensor, lex_label_mask : torch.Tensor,
                 term_types : Optional[torch.Tensor], term_type_mask : Optional[torch.Tensor],
                 heads : Optional[torch.Tensor],
                 context : Dict[str, torch.Tensor], children_label_rep : torch.Tensor) -> torch.Tensor:
        """
        Computes the loss.
        :param children_label_rep:
        :param heads: shape (batch_size, number of tokens), no token for artificial root node included.
        :param term_types: (batch_size, decision_seq_len)
        :param lex_label_mask: (batch_size, decision_seq_len) whether there is a decision to be made for the lexical label.
        :param lex_labels: (batch_size, decision_seq_len, vocab size) which lexical label to pick for each decision
        :param supertag_mask: (batch_size, decision_seq_len) indicating where supertags should be predicted.
        :param supertags: shape (batch_size, decision_seq_len, supertag vocab) which supertag to pick for each decision
        :param context: a dictionary with key describing the context (parents, siblings) and values of shape (batch_size, decision seq len, *)
            with additional information for each decision.
        :param active_nodes: shape (batch_size, input_seq_len) with currently active node (e.g. top of stack)
        :param state: state of lstm
        :param seq: shape (batch_size, decision_seq_len) with indices which elements to pick
        :param labels: (batch_size, decision_seq_len) gold edge labels
        :param label_mask: (batch_size, decision_seq_len) indicating where edge labels should be predicted
        :return: a tensor of shape (batch_size,) with the loss
        """

        batch_size, output_seq_len = seq.shape

        sentence_loss = self.compute_sentence_loss(state, seq, active_nodes,
                                                   labels, label_mask,
                                                   supertags, supertag_mask,
                                                   lex_labels, lex_label_mask,
                                                   term_types, term_type_mask,
                                                   heads, context, children_label_rep) # shape (batch_size,)
        sentence_loss = sentence_loss.sum() #shape (1,)

        dm_loss = sentence_loss.new_zeros(1)
        if "additional_edge_scores" in state:
            heads = torch.cat([heads.new_zeros(batch_size, 1), heads], 1) # shape (batch_size, input_seq_len) now contains artificial root
            dm_loss = dm_edge_loss.loss(state["additional_edge_scores"], heads, state["input_mask"])

            predicted_heads = self.additional_edge_model.greedy_decode_arcs(state["additional_edge_scores"], state["input_mask"])

            self.heads_correct += (state["input_mask"] * (predicted_heads == heads)).sum().cpu().numpy() # TODO: remove artificial root
            self.heads_predicted += torch.sum(state["input_mask"]).cpu().numpy()

        return (sentence_loss + dm_loss) / batch_size


    def compute_sentence_loss(self, state: Dict[str, torch.Tensor], seq: torch.Tensor, active_nodes : torch.Tensor,
                          labels: torch.Tensor, label_mask: torch.Tensor,
                          supertags : torch.Tensor, supertag_mask : torch.Tensor,
                          lex_labels : torch.Tensor, lex_label_mask : torch.Tensor,
                          term_types : Optional[torch.Tensor], term_type_mask : Optional[torch.Tensor],
                          heads : Optional[torch.Tensor],
                          context : Dict[str, torch.Tensor], children_label_rep : torch.Tensor) -> torch.Tensor:
        """
        Computes the loss.
        :type children_label_rep: torch.Tensor of shape (batch size, number of tokens, embedding dim).
        :param heads: shape (batch_size, number of tokens), no token for artificial root node included.
        :param term_types: (batch_size, decision_seq_len)
        :param lex_label_mask: (batch_size, decision_seq_len) whether there is a decision to be made for the lexical label.
        :param lex_labels: (batch_size, decision_seq_len, vocab size) which lexical label to pick for each decision
        :param supertag_mask: (batch_size, decision_seq_len) indicating where supertags should be predicted.
        :param supertags: shape (batch_size, decision_seq_len, supertag vocab) which supertag to pick for each decision
        :param context: a dictionary with key describing the context (parents, siblings) and values of shape (batch_size, decision seq len, *)
            with additional information for each decision.
        :param active_nodes: shape (batch_size, input_seq_len) with currently active node (e.g. top of stack)
        :param state: state of lstm
        :param seq: shape (batch_size, decision_seq_len) with indices which elements to pick
        :param labels: (batch_size, decision_seq_len) gold edge labels
        :param label_mask: (batch_size, decision_seq_len) indicating where edge labels should be predicted
        :return: a tensor of shape (batch_size,) with the loss
        """
        self.init_decoder(state)
        batch_size, output_seq_len = seq.shape
        _, input_seq_len, encoder_dim = state["encoded_input"].shape

        self.common_setup_decode(state)

        bool_label_mask = label_mask.bool() #to speed things a little up
        bool_supertag_mask = supertag_mask.bool()


        loss = torch.zeros(batch_size, device=get_device_id(seq))

        assert torch.all(seq[:, 0] == 0), "The first node in the traversal must be the artificial root with index 0"

        if self.context_provider:
            undo_one_batching(context)

        #Dropout mask for output of decoder
        ones = loss.new_ones((batch_size, self.decoder_output_dim))
        dropout_mask = torch.nn.functional.dropout(ones, self.encoder_output_dropout_rate, self.training, inplace=False)

        range_batch_size = get_range_vector(batch_size, get_device_id(seq)) # replaces range(batch_size)

        for step in range(output_seq_len - 1):
            # Retrieve current vector corresponding to current node and feed to decoder
            current_node = active_nodes[:, step]
            assert current_node.shape == (batch_size,)

            encoding_current_node = state["encoded_input"][range_batch_size, current_node] # (batch_size, encoder dim)
            encoding_current_node_tagging = state["encoded_input_for_tagging"][range_batch_size, current_node]

            if self.context_provider:
                # Generate context snapshot of current time-step.
                current_context = { feature_name : tensor[:, step] for feature_name, tensor in context.items()}
            else:
                current_context = dict()

            decoder_hidden, decoder_hidden_tagging = self.decoder_step(state, current_node, encoding_current_node, encoding_current_node_tagging, current_context)

            # if self.context_provider:
            #     # Generate context snapshot of current time-step.
            #     current_context = { feature_name : tensor[:, step] for feature_name, tensor in context.items()}
            #     encoding_current_node = self.context_provider.forward(encoding_current_node, state, current_context)
            #
            # self.decoder.step(encoding_current_node)
            # decoder_hidden = self.decoder.get_hidden_state()
            assert decoder_hidden.shape == (batch_size, self.decoder_output_dim)
            decoder_hidden = decoder_hidden * dropout_mask

            #####################
            # Predict edges
            edge_scores = self.edge_model.edge_scores(decoder_hidden)
            assert edge_scores.shape == (batch_size, input_seq_len)

            # Get target of gold edges
            target_gold_edges = seq[:, step + 1]
            assert target_gold_edges.shape == (batch_size,)

            # Compute edge existence loss
            current_mask = seq[:, step+1] >= 0 # are we already in the padding region?
            #target_gold_edges = F.relu(target_gold_edges)

            max_values, argmax = torch.max(edge_scores, dim=1) # shape (batch_size,)
            self.head_decisions_correct += torch.sum(current_mask * (target_gold_edges == argmax)).cpu().numpy()
            self.decisions += torch.sum(current_mask).cpu().numpy()

            #loss = loss + current_mask * (edge_scores[range_batch_size, target_gold_edges] - max_values) #TODO: check no log_softmax! TODO margin.
            #loss = loss + current_mask * edge_scores[range_batch_size, target_gold_edges]
            loss = loss + self.edge_loss.compute_loss(edge_scores, target_gold_edges, current_mask, state["input_mask"])

            #####################

            if torch.any(bool_label_mask[:, step + 1]):
                # Compute edge label scores
                edge_label_scores = self.edge_label_model.edge_label_scores(target_gold_edges, decoder_hidden)
                assert edge_label_scores.shape == (batch_size, self.edge_label_model.vocab_size)

                gold_labels = labels[:, step + 1]
                edge_loss = edge_label_scores[range_batch_size, gold_labels]
                assert edge_loss.shape == (batch_size,)

                # We don't have to predict an edge label everywhere, so apply the appropriate mask:
                loss = loss + label_mask[:, step + 1] * edge_loss
                #loss = loss + label_mask[:, step + 1] * F.nll_loss(edge_label_scores, gold_labels, reduction="none")

            #####################
            if self.transition_system.predict_supertag_from_tos():
                relevant_nodes_for_supertagging = current_node
            else:
                relevant_nodes_for_supertagging = target_gold_edges

            decoder_hidden_tagging_with_label_rep = torch.cat((decoder_hidden_tagging, children_label_rep[range_batch_size, relevant_nodes_for_supertagging]), dim=1)

            # Compute supertagging loss
            if self.supertagger is not None and torch.any(bool_supertag_mask[:, step+1]):
                supertagging_loss, supertags_correct, supertag_decisions = \
                    self.compute_tagging_loss(self.supertagger, decoder_hidden_tagging_with_label_rep, relevant_nodes_for_supertagging, supertag_mask[:, step+1], supertags[:, step+1])

                self.supertags_correct += supertags_correct
                self.supertag_decisions += supertag_decisions

                loss = loss - supertagging_loss

            # Compute lex label loss:
            if self.lex_label_tagger is not None and lex_labels is not None:
                lexlabel_loss, lex_labels_correct, lex_label_decisions = \
                    self.compute_tagging_loss(self.lex_label_tagger, decoder_hidden_tagging, relevant_nodes_for_supertagging, lex_label_mask[:, step+1], lex_labels[:, step+1])

                self.lex_labels_correct += lex_labels_correct
                self.lex_label_decisions += lex_label_decisions

                loss = loss - lexlabel_loss

            if self.term_type_tagger is not None and term_types is not None:
                term_type_loss, term_types_correct, term_type_decisions = \
                    self.compute_tagging_loss(self.term_type_tagger, decoder_hidden_tagging_with_label_rep, relevant_nodes_for_supertagging, term_type_mask[:, step+1], term_types[:, step+1])

                self.term_types_correct += term_types_correct
                self.term_type_decisions += term_type_decisions

                loss = loss - term_type_loss

        return -loss

    def parse_sentences(self, transition_system : TransitionSystem, state: Dict[str, torch.Tensor], formalism : str, sentences: List[AMSentence],
                        children_label_rep : torch.Tensor) -> List[AMSentence]:
        """
        Parses the sentences.
        :param sentences:
        :param state:
        :return:
        """
        self.init_decoder(state)
        batch_size, input_seq_len, encoder_dim = state["encoded_input"].shape
        device = get_device_id(state["encoded_input"])

        self.common_setup_decode(state)

        INF = 10e10
        inverted_input_mask = INF * (1-state["input_mask"]) #shape (batch_size, input_seq_len)

        output_seq_len = input_seq_len*2 + 1

        next_active_nodes = torch.zeros(batch_size, dtype=torch.long, device = device) #start with artificial root.

        range_batch_size = get_range_vector(batch_size, device)

        parsing_states = [transition_system.initial_state(sentence, None) for sentence in sentences]

        for step in range(output_seq_len):
            encoding_current_node = state["encoded_input"][range_batch_size, next_active_nodes]
            encoding_current_node_tagging = state["encoded_input_for_tagging"][range_batch_size, next_active_nodes]

            if self.context_provider:
                # Generate context snapshot of current time-step.
                current_context : List[Dict[str, torch.Tensor]] = [parsing_states[i].gather_context(device) for i in range(batch_size)]
                current_context = batch_and_pad_tensor_dict(current_context)
                undo_one_batching_eval(current_context)
            else:
                current_context = dict()

            decoder_hidden, decoder_hidden_tagging = self.decoder_step(state, next_active_nodes,encoding_current_node, encoding_current_node_tagging, current_context)

            assert decoder_hidden.shape == (batch_size, self.decoder_output_dim)

            #####################
            # Predict edges
            edge_scores = self.edge_model.edge_scores(decoder_hidden)
            assert edge_scores.shape == (batch_size, input_seq_len)

            # Apply filtering of valid choices:
            edge_scores = edge_scores - inverted_input_mask #- INF*(1-valid_choices)

            selected_nodes = torch.argmax(edge_scores, dim=1)
            # assert selected_nodes.shape == (batch_size,)
            # all_selected_nodes.append(selected_nodes)

            #####################
            scores : Dict[str, torch.Tensor] = {"children_scores": edge_scores, "max_children" : selected_nodes.unsqueeze(1),
                                                "inverted_input_mask" : inverted_input_mask}

            # Compute edge label scores, perhaps they are useful to parsing procedure, perhaps it has to recompute.
            edge_label_scores = self.edge_label_model.edge_label_scores(selected_nodes, decoder_hidden)
            assert edge_label_scores.shape == (batch_size, self.edge_label_model.vocab_size)

            scores["edge_labels_scores"] = F.log_softmax(edge_label_scores,1)

            #####################
            if transition_system.predict_supertag_from_tos():
                relevant_nodes_for_supertagging = next_active_nodes
            else:
                relevant_nodes_for_supertagging = selected_nodes

            if children_label_rep is not None:
                #(batch_size, decoder dim) + (batch_size, embedding dim)
                decoder_hidden_tagging_with_label_rep = torch.cat((decoder_hidden_tagging, children_label_rep[range_batch_size, relevant_nodes_for_supertagging]), dim=1)

            #Compute supertags:
            if self.supertagger is not None and children_label_rep is not None:
                supertag_scores = self.supertagger.tag_scores(decoder_hidden_tagging_with_label_rep, relevant_nodes_for_supertagging)
                assert supertag_scores.shape == (batch_size, self.supertagger.vocab_size)

                scores["constants_scores"] = F.log_softmax(supertag_scores,1)

            if self.lex_label_tagger is not None:
                lex_label_scores = self.lex_label_tagger.tag_scores(decoder_hidden_tagging, relevant_nodes_for_supertagging)
                assert lex_label_scores.shape == (batch_size, self.lex_label_tagger.vocab_size)

                scores["lex_labels_scores"] = F.log_softmax(lex_label_scores,1)

            if self.term_type_tagger is not None and children_label_rep is not None:
                term_type_scores = self.term_type_tagger.tag_scores(decoder_hidden_tagging_with_label_rep, relevant_nodes_for_supertagging)
                assert term_type_scores.shape == (batch_size, self.term_type_tagger.vocab_size)

                scores["term_types_scores"] = F.log_softmax(term_type_scores, 1)

            ### Update current node according to transition system:
            active_nodes = []
            for i, parsing_state in enumerate(parsing_states):
                decision = transition_system.make_decision(index_tensor_dict(scores,i), self.edge_label_model, parsing_state)
                parsing_states[i] = transition_system.step(parsing_state, decision, in_place = True)
                active_nodes.append(parsing_states[i].active_node)

            next_active_nodes = torch.tensor(active_nodes, dtype=torch.long, device=device)

            assert next_active_nodes.shape == (batch_size,)

            if all(parsing_state.is_complete() for parsing_state in parsing_states):
                break

        return [state.extract_tree() for state in parsing_states]


    def beam_search(self, transition_system: TransitionSystem, encoder_state: Dict[str, torch.Tensor], sentences: List[AMSentence], k : int,
                    children_label_rep : torch.Tensor) -> List[AMSentence]:
        """
        Parses the sentences.
        :param sentences:
        :param encoder_state:
        :return:
        """
        batch_size, input_seq_len, encoder_dim = encoder_state["encoded_input"].shape
        device = get_device_id(encoder_state["encoded_input"])

        # Every key in encoder_state has the dimensions (batch_size, ....)
        # We now replace that by (k*batch_size, ...) where we repeat each batch element k times.
        #old_dict = dict(encoder_state)
        encoder_states_list = expand_tensor_dict(encoder_state)
        k_times = []
        for s in encoder_states_list:
            for _ in range(k):
                k_times.append(s)
        encoder_state = batch_tensor_dicts(k_times) # shape (k*batch_size, ...)

        #assert all(torch.all(old_dict[k] == encoder_state[k]) for k in old_dict.keys())

        self.init_decoder(encoder_state)
        self.common_setup_decode(encoder_state)

        INF = 10e10
        inverted_input_mask = INF * (1 - encoder_state["input_mask"]) #shape (batch_size, input_seq_len)

        output_seq_len = input_seq_len*2 + 1

        next_active_nodes = torch.zeros(k*batch_size, dtype=torch.long, device = device) #start with artificial root.

        range_batch_size = get_range_vector(k*batch_size, device)

        parsing_states : List[List[ParsingState]] = []
        decoder_states_full = self.decoder.get_full_states()
        for i, sentence in enumerate(sentences):
            parsing_states.append([transition_system.initial_state(sentence, decoder_states_full[k*i+p]) for p in range(k)])

        assumes_greedy_ok = transition_system.assumes_greedy_ok()
        condition_on = set()
        if self.context_provider is not None:
            condition_on = set(self.context_provider.conditions_on())
        if self.tagger_context_provider is not None:
            condition_on.update(self.tagger_context_provider.conditions_on())

        if len(assumes_greedy_ok & condition_on):
            raise ConfigurationError(f"You chose a beam search algorithm that assumes making greedy decisions in terms of {assumes_greedy_ok}"
                                     f"won't impact future decisions but your context provider includes information about {condition_on}")

        for step in range(output_seq_len):
            encoding_current_node = encoder_state["encoded_input"][range_batch_size, next_active_nodes]
            encoding_current_node_tagging = encoder_state["encoded_input_for_tagging"][range_batch_size, next_active_nodes]

            if self.context_provider:
                # Generate context snapshot of current time-step.
                current_context : List[Dict[str, torch.Tensor]] = []
                for sentence_states in parsing_states:
                    current_context.extend([state.gather_context(device) for state in sentence_states])
                current_context = batch_and_pad_tensor_dict(current_context)
                undo_one_batching_eval(current_context)
            else:
                current_context = dict()

            decoder_hidden, decoder_hidden_tagging = self.decoder_step(encoder_state, next_active_nodes, encoding_current_node, encoding_current_node_tagging, current_context)

            assert decoder_hidden.shape == (k*batch_size, self.decoder_output_dim)

            #####################
            # Predict edges
            edge_scores = self.edge_model.edge_scores(decoder_hidden)
            assert edge_scores.shape == (k*batch_size, input_seq_len)

            edge_scores = F.log_softmax(edge_scores,1)
            # Apply filtering of valid choices:
            edge_scores = edge_scores - inverted_input_mask #- INF*(1-valid_choices)

            selected_nodes = torch.argmax(edge_scores, dim=1)
            # assert selected_nodes.shape == (k*batch_size,)
            # all_selected_nodes.append(selected_nodes)

            #####################
            scores : Dict[str, torch.Tensor] = {"children_scores": edge_scores, "max_children" : selected_nodes.unsqueeze(1),
                                                "inverted_input_mask" : inverted_input_mask}

            #####################
            if transition_system.predict_supertag_from_tos():
                relevant_nodes_for_supertagging = next_active_nodes
            else:
                relevant_nodes_for_supertagging = selected_nodes

            if children_label_rep is not None:
                #(batch_size, decoder dim) + (batch_size, embedding dim)
                decoder_hidden_tagging_with_label_rep = torch.cat((decoder_hidden_tagging, children_label_rep[range_batch_size, relevant_nodes_for_supertagging]), dim=1)

            #Compute supertags:
            if self.supertagger is not None and decoder_hidden_tagging_with_label_rep is not None:
                supertag_scores = self.supertagger.tag_scores(decoder_hidden_tagging_with_label_rep, relevant_nodes_for_supertagging)
                assert supertag_scores.shape == (k*batch_size, self.supertagger.vocab_size)

                scores["constants_scores"] = F.log_softmax(supertag_scores,1)

            if self.lex_label_tagger is not None:
                lex_label_scores = self.lex_label_tagger.tag_scores(decoder_hidden_tagging, relevant_nodes_for_supertagging)
                assert lex_label_scores.shape == (k*batch_size, self.lex_label_tagger.vocab_size)

                scores["lex_labels_scores"] = F.log_softmax(lex_label_scores,1)

            if self.term_type_tagger is not None and decoder_hidden_tagging_with_label_rep is not None:
                term_type_scores = self.term_type_tagger.tag_scores(decoder_hidden_tagging_with_label_rep, relevant_nodes_for_supertagging)
                assert term_type_scores.shape == (k*batch_size, self.term_type_tagger.vocab_size)

                scores["term_types_scores"] = F.log_softmax(term_type_scores, 1)

            encoder_state["decoder_hidden"] = decoder_hidden
            decoder_states_full = self.decoder.get_full_states()
            ### Update current node according to transition system:
            active_nodes = []
            for sentence_id, sentence in enumerate(parsing_states):
                all_decisions_for_sentence = []
                for i, parsing_state in enumerate(sentence):
                    parsing_state.decoder_state = decoder_states_full[k*sentence_id+i]
                    top_k : List[Decision] = transition_system.top_k_decision(index_tensor_dict(scores,k*sentence_id+i),
                                                                                   index_tensor_dict(encoder_state,k*sentence_id+i),
                                                                                   self.edge_label_model, parsing_state, k)
                    for decision in top_k:
                        all_decisions_for_sentence.append((decision, parsing_state))

                    if step == 0:
                        # in the first step, we always have that parsing_state is the initial state for that sentence
                        # and the scores are identical.
                        break
                # Find top k overall decisions
                #all_decisions_for_sentence = sorted(all_decisions_for_sentence, reverse=True, key=lambda decision_and_state: decision_and_state[0].score + decision_and_state[1].score)
                top_k_decisions = heapq.nlargest(k, all_decisions_for_sentence, key=lambda decision_and_state: decision_and_state[0].score + decision_and_state[1].score)
                for decision_nr, (decision, parsing_state) in enumerate(top_k_decisions):
                    next_parsing_state = transition_system.step(parsing_state, decision, in_place = False)
                    parsing_states[sentence_id][decision_nr] = next_parsing_state
                    active_nodes.append(next_parsing_state.active_node)

                for _ in range(k-len(top_k_decisions)): #there weren't enough decisions, fill with some parsing state
                    active_nodes.append(0)


            next_active_nodes = torch.tensor(active_nodes, dtype=torch.long, device=device)
            # Bring decoder network into correct state
            decoder_states_full = []
            for sentence in parsing_states:
                for parsing_state in sentence:
                    decoder_states_full.append(parsing_state.decoder_state)
            self.decoder.set_with_full_states(decoder_states_full)

            assert next_active_nodes.shape == (k*batch_size,)

            if all(all(s.is_complete() for s in sentennce_states) for sentennce_states in parsing_states):
                break

        ret = []
        for sentence in parsing_states:
            best_state = max(sentence, key=lambda state: state.score)
            ret.append(best_state.extract_tree())

        return ret