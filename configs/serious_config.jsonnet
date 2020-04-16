local num_epochs = 100;
local device = 0;

local word_dim = 200;
local pos_embedding = 32;

local encoder_dim = 512;
local batch_size = 64;

local char_dim = 100;
local num_filters = 50;
local filters = [3];
local max_filter = 3; //KEEP IN SYNC WITH filters!


local glove_dir = "/local/mlinde/glove/";


local eval_commands = import "eval_commands.libsonnet";

local additional_lexicon = {
     "sublexica" : {
            "edge_labels" : "data/AMR/2015/train/edges.txt",
            "lexical_types" : "data/AMR/2015/train/types.txt"
     }
} ;

local transition_system = {
    "type" : "dfs",
//    "type" : "dfs-children-first",
    "children_order" : "IO",
    "pop_with_0" : true,
    "additional_lexicon" : additional_lexicon,
};

local formalism = "amr";

local dataset_reader = {
               "type": "amconll",
               "transition_system" : transition_system,
               "workers" : 4,
               "overwrite_formalism" : formalism,

              "token_indexers" : {
                   "tokens" : {
                       "type": "single_id",
                        "lowercase_tokens": true
                   },
                   "token_characters" : {
                       "type" : "characters",
                       "min_padding_length" : max_filter
                   }
              }

           };

local data_iterator = {
        "type": "same_formalism",
        "batch_size": batch_size,
       "formalisms" : [formalism]
    };


{
    "dataset_reader": dataset_reader,
    "validation_dataset_reader" : dataset_reader,

    "validation_command" : {

        "type" : "bash_evaluation_command",
        "command" : "python topdown_parser/evaluation/am_dep_las.py {gold_file} {system_output}",

        "result_regexes" : {
            "Constant_Acc" : [4, "Supertagging acc % (?P<value>[0-9.]+)"],
            "Lex_Acc" : [5, "Lexical label acc % (?P<value>[0-9.]+)"],
            "UAS" : [6, "UAS.* % (?P<value>[0-9.]+)"],
            "LAS" : [7, "LAS.* % (?P<value>[0-9.]+)"],
            "Content_recall" : [8, "Content recall % (?P<value>[0-9.]+)"]
        }
    },



    "iterator": data_iterator,
    "model": {
        "type": "topdown",
        "transition_system" : transition_system,

        "context_provider" : {
            "type" : "sum",
            "providers" : [
//                  {"type" : "type-embedder", "hidden_dim" : 2*encoder_dim, "additional_lexicon" : additional_lexicon }
                  {"type" : "most-recent-child" }

//                {"type" : "label-embedder",
//                    "additional_lexicon" : additional_lexicon,
//                    "hidden_dim" : 2*encoder_dim,
//                    "dropout" : 0.2
//                }
            ]
        },

        "input_dropout" : 0.33,
        "encoder_output_dropout" : 0.33,

            "supertagger" : {
//                "type" : "simple-tagger",
                "type" : "no-decoder-tagger",
                "formalism" : "amr",
                "suffix_namespace" : "supertags",
                "mlp" : {
                    "input_dim" : 2*encoder_dim,
                    "num_layers" : 1,
                    "hidden_dims" : 1024,
                    "dropout" : 0.4,
                    "activations" : "tanh",
                }
            },

            "lex_label_tagger" : {
//                "type" : "simple-tagger",
                "type" : "no-decoder-tagger",
                "formalism" : "amr",
                "suffix_namespace" : "lex_labels",
                "mlp" : {
                    "input_dim" : 2*encoder_dim,
                    "num_layers" : 1,
                    "hidden_dims" : 1024,
                    "dropout" : 0.4,
                    "activations" : "tanh",
                }
            },

        "encoder" : {
             "type": "stacked_bidirectional_lstm",
            "input_size": num_filters + word_dim + pos_embedding,
            "hidden_size": encoder_dim,
            "num_layers" : 3,
            "recurrent_dropout_probability" : 0.33,
            "layer_dropout_probability" : 0.33
        },

        "tagger_encoder" : {
             "type": "stacked_bidirectional_lstm",
            "input_size": num_filters + word_dim + pos_embedding,
            "hidden_size": encoder_dim,
            "num_layers" : 2,
            "recurrent_dropout_probability" : 0.33,
            "layer_dropout_probability" : 0.33
        },

        "decoder" : {
            "type" : "ma-lstm",
            "input_dim": 2*encoder_dim,
            "hidden_dim" : 2*encoder_dim,
            "input_dropout" : 0.33,
            "recurrent_dropout" : 0.33
        },
        "text_field_embedder": {
               "tokens": {
                    "type": "embedding",
                    "embedding_dim": word_dim,
                    "pretrained_file": glove_dir+"glove.6B.200d.txt"
                },
                "token_characters": {
                  "type": "character_encoding",
                      "embedding": {
                        "embedding_dim": char_dim
                      },
                      "encoder": {
                        "type": "cnn",
                        "embedding_dim": char_dim,
                        "num_filters": num_filters,
                        "ngram_filter_sizes": filters
                      }
                }
        },

        "edge_model" : {
            "type" : "ma",
            "mlp" : {
                    "input_dim" : 2*encoder_dim,
                    "num_layers" : 1,
                    "hidden_dims" : 512,
                    "activations" : "elu",
                    "dropout" : 0.33
            }
        },

        "edge_label_model" : {
            "type" : "simple",
            "formalism" : formalism,
            "mlp" : {
                "input_dim" : 2*2*encoder_dim,
                "num_layers" : 1,
                "hidden_dims" : [256],
                "activations" : "tanh",
                "dropout" : 0.33
            }
        },

        "edge_loss" : {
            "type" : "nll"
        },

        "pos_tag_embedding" : {
            "embedding_dim" : pos_embedding,
            "vocab_namespace" : "pos"
        }

    },
    "train_data_path": "data/AMR/2015/train/train.amconll",
    "validation_data_path": "data/AMR/2015/gold-dev/gold-dev.amconll",

    "evaluate_on_test" : false,

    "trainer": {
        "num_epochs": num_epochs,
        "cuda_device": device,
        "optimizer": {
            "type": "adam",
            "betas" : [0.9, 0.9]
        },
        "num_serialized_models_to_keep" : 1,
        "validation_metric" : "+LAS"
    },

    "dataset_writer":{
      "type":"amconll_writer"
    },

    "annotator" : {
        "dataset_reader": dataset_reader,
        "data_iterator": data_iterator,
        "dataset_writer":{
              "type":"amconll_writer"
        }
    },

    "callbacks" : eval_commands["AMR-2015"]
}
