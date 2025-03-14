#!/usr/bin/env/python
import json
import os
import pickle
import random
import time
from collections import defaultdict
from typing import List, Any

import numpy as np
import tensorflow as tf

import utils
from model.histManager import HistManager as HM
from utils.utils import dataset_info, ThreadedIterator


def safe_dataset(x: str) -> str:
    return x.replace("/", "-").replace(".", "_")


class ChemModel(object):
    @classmethod
    def default_params(cls):
        return {}

    def __init__(self, args):
        self.args = args

        # Collect argument things:
        data_dir = ""
        if "--data_dir" in args and args["--data_dir"] is not None:
            data_dir = args["--data_dir"]
        self.data_dir = data_dir

        # Collect parameters:
        params = self.default_params()
        config_file = args.get("--config-file")
        if config_file is not None:
            with open(config_file, "r") as f:
                params.update(json.load(f))
        config = args.get("--config")
        if config is not None:
            params.update(json.loads(config))
        self.params = params
        # adjust variables values
        if self.params["generation"] == 2:  # for reconstruction
            self.params["try_different_starting"] = False
            self.params["use_argmax_nodes"] = True
            self.params["use_argmax_bonds"] = True

        if self.params["generation"] == 3:  # for testing
            self.params["try_different_starting"] = False
            self.params["use_argmax_nodes"] = True
            self.params["use_argmax_bonds"] = True

        # use only cpu
        if not self.params["use_gpu"]:
            os.environ["CUDA_VISIBLE_DEVICES"] = "-1"

        # Get which dataset in use
        self.params["dataset"] = dataset = args.get("--dataset")
        # Number of atom types of this dataset
        self.params["num_symbols"] = len(dataset_info(dataset)["atom_types"])

        suff = "_" + self.params["suffix"] if self.params["suffix"] is not None else ""
        self.run_id = "_".join([time.strftime("%Y-%m-%d-%H-%M-%S"), str(os.getpid())])
        log_dir = self.params["log_dir"]
        self.log_file = os.path.join(
            log_dir,
            "%s_log_%s%s.json" % (self.run_id, safe_dataset(dataset), suff),
        )
        self.best_model_file = os.path.join(
            log_dir, "%s_model%s.pickle" % (self.run_id, suff)
        )

        with open(
            os.path.join(
                log_dir,
                "%s_params_%s%s.json" % (self.run_id, dataset.replace("/", "_"), suff),
            ),
            "w",
        ) as f:
            json.dump(params, f)

        print(
            "Run %s starting with following parameters:\n%s"
            % (self.run_id, json.dumps(self.params))
        )

        # Set random seeds
        random.seed(params["random_seed"])
        np.random.seed(params["random_seed"])

        # Load data:
        self.max_num_vertices = 0
        self.num_edge_types = 0
        self.annotation_size = 0
        if self.params["generation"] == 0:
            train_data, self.train_data = self.load_data(
                params["train_file"], is_training_data=True
            )
        else:
            train_data, self.train_data = self.load_data(
                params["train_file"], is_training_data=False
            )
        valid_data, self.valid_data = self.load_data(
            params["valid_file"], is_training_data=False
        )
        test_data, self.test_data = self.load_data(
            params["test_file"], is_training_data=False
        )
        print(
            len(train_data),
            len(valid_data),
            len(test_data),
        )
        self.histograms = dict()
        self.histograms["hist_dim"] = utils.dataset_info(self.params["dataset"])[
            "hist_dim"
        ]
        self.histograms["max_valence"] = utils.dataset_info(self.params["dataset"])[
            "max_valence_value"
        ]
        self.max_num_vertices = dataset_info(dataset)["max_n_atoms"]
        self.histograms["train"] = self.prepareHist(train_data)
        # A = number of atoms in a molecule, N = number of histograms
        # With filter we create a list of max(A) lists, which each list inside the main one are the weights for each histogram
        # according to the number of atoms
        # 0 return the frequency, 1 return the prob
        self.histograms["filter"] = HM.v_filter(
            self.histograms["train"][0],
            self.histograms["train"][1],
            self.max_num_vertices,
        )
        self.histograms["valid"] = self.prepareHist(valid_data)
        self.histograms["test"] = self.prepareHist(test_data)
        # print(self.histograms['filter'])

        # Build the actual model
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        self.graph = tf.Graph()
        self.sess = tf.Session(graph=self.graph, config=config)
        with self.graph.as_default():
            tf.set_random_seed(params["random_seed"])
            self.placeholders = {}
            self.weights = {}
            self.ops = {}

            self.make_model()
            self.make_train_step()

            # Restore/initialize variables:
            restore_file = args.get("--restore")
            if restore_file is not None:
                self.restore_model(restore_file)
            else:
                self.initialize_model()

    def load_data(self, file_name, is_training_data: bool):
        full_path = os.path.join(self.data_dir, file_name)

        print("Loading data from %s" % full_path)

        with open(full_path, "r") as f:
            data = json.load(f)

        restrict = self.args.get("--restrict_data")
        if restrict is not None and 0 < float(restrict) < 1:
            idx = np.random.randint(
                0, high=len(data), size=round(len(data) * float(restrict))
            )
            data = [data[i] for i in idx]

        # Get some common data out:
        num_fwd_edge_types = len(utils.bond_dict) - 1
        for g in data:
            self.max_num_vertices = max(
                self.max_num_vertices,
                max([v for e in g["graph"] for v in [e[0], e[2]]]),
            )

        self.num_edge_types = max(
            self.num_edge_types,
            num_fwd_edge_types * (1 if self.params["tie_fwd_bkwd"] else 2),
        )

        self.annotation_size = max(
            self.annotation_size, len(data[0]["node_features"][0])
        )

        return data, self.process_raw_graphs(data, is_training_data, file_name)

    """
    Return:
    - array_hist contains all the unique set of histograms
    - array_number contains the number of the molecules with the same histogram
    """

    def prepareHist(self, data):
        diz = defaultdict(list)
        for i in data:
            key = HM.histToScore(i["hist"], self.histograms["max_valence"])
            diz[key].append(1)  # just a placeholder

        array_number = []
        array_hist = []
        for hist in sorted(diz.keys()):
            array_number.append(len(diz[hist]))
            array_hist.append(
                HM.scoreToHist(
                    hist, self.histograms["hist_dim"], self.histograms["max_valence"]
                )
            )

        return [array_hist, array_number]

    @staticmethod
    def graph_string_to_array(graph_string: str) -> List[List[int]]:
        return [[int(v) for v in s.split(" ")] for s in graph_string.split("\n")]

    def process_raw_graphs(
        self, raw_data, is_training_data, file_name, bucket_sizes=None
    ):
        raise Exception("Models have to implement process_raw_graphs!")

    def make_model(self):
        self.placeholders["target_values"] = tf.placeholder(
            tf.float32, [len(self.params["task_ids"]), None], name="target_values"
        )
        self.placeholders["target_mask"] = tf.placeholder(
            tf.float32, [len(self.params["task_ids"]), None], name="target_mask"
        )
        self.placeholders["num_graphs"] = tf.placeholder(
            tf.int32, [], name="num_graphs"
        )
        self.placeholders["out_layer_dropout_keep_prob"] = tf.placeholder(
            tf.float32, [], name="out_layer_dropout_keep_prob"
        )
        # whether this session is for generating new graphs or not
        self.placeholders["is_generative"] = tf.placeholder(
            tf.bool, [], name="is_generative"
        )
        self.placeholders["use_teacher_forcing_nodes"] = tf.placeholder(
            tf.bool, None, name="use_teacher_forcing_nodes"
        )

        with tf.variable_scope("graph_model"):
            self.prepare_specific_graph_model()

            initial_state = self.get_node_embedding_state(
                self.placeholders["node_symbols"]
            )

            # This does the actual graph work:
            if self.params["use_graph"]:
                if self.params["residual_connection_on"]:
                    self.ops[
                        "final_node_representations"
                    ] = self.compute_final_node_representations_with_residual(
                        initial_state,
                        tf.transpose(
                            self.placeholders["adjacency_matrix"], [1, 0, 2, 3]
                        ),
                        "_encoder",
                    )
                else:
                    self.ops[
                        "final_node_representations"
                    ] = self.compute_final_node_representations_without_residual(
                        initial_state,
                        tf.transpose(
                            self.placeholders["adjacency_matrix"], [1, 0, 2, 3]
                        ),
                        self.weights["edge_weights_encoder"],
                        self.weights["edge_biases_encoder"],
                        self.weights["node_gru_encoder"],
                        "gru_scope_encoder",
                    )
            else:
                self.ops["final_node_representations"] = initial_state

        # Calculate p(z|x)'s mean and log variance
        self.compute_mean_and_logvariance()

        # Sample from a gaussian distribution according to the mean and log variance
        self.sample_with_mean_and_logvariance()

        # obtains te latent representation of the nodes. This is the decoder's first part
        # it always use the NN function without teacher forcing
        self.construct_nodes()

        # Construct logit matrices for both edges and edge types. This is the decoder's second part
        # it uses teacher forcing in the training
        self.construct_logit_matrices()

        # Obtain losses for edges and edge types
        self.ops["qed_loss"] = []
        self.ops["loss"] = self.construct_loss()

    def make_train_step(self):
        trainable_vars = self.sess.graph.get_collection(
            tf.GraphKeys.TRAINABLE_VARIABLES
        )
        if self.args.get("--freeze-graph-model"):
            graph_vars = set(
                self.sess.graph.get_collection(
                    tf.GraphKeys.TRAINABLE_VARIABLES, scope="graph_model"
                )
            )
            filtered_vars = []
            for var in trainable_vars:
                if var not in graph_vars:
                    filtered_vars.append(var)
                else:
                    print("Freezing weights of variable %s." % var.name)
            trainable_vars = filtered_vars

        optimizer = tf.train.AdamOptimizer(self.params["learning_rate"])
        grads_and_vars = optimizer.compute_gradients(
            self.ops["loss"], var_list=trainable_vars
        )

        clipped_grads = []
        for grad, var in grads_and_vars:
            if grad is not None:
                clipped_grads.append(
                    (tf.clip_by_norm(grad, self.params["clamp_gradient_norm"]), var)
                )
            else:
                clipped_grads.append((grad, var))
        grads_for_display = []
        grads_for_display2 = []
        for grad, var in grads_and_vars:
            if grad is not None:
                grads_for_display.append(
                    (tf.clip_by_norm(grad, self.params["clamp_gradient_norm"]), var)
                )
                grads_for_display2.append(grad)
        self.ops["grads"] = grads_for_display
        self.ops["grads2"] = grads_for_display2
        self.ops["train_step"] = optimizer.apply_gradients(clipped_grads)
        # Initialize newly-introduced variables:
        self.sess.run(tf.local_variables_initializer())

    def gated_regression(self, last_h, regression_gate, regression_transform):
        raise Exception("Models have to implement gated_regression!")

    def prepare_specific_graph_model(self) -> None:
        raise Exception("Models have to implement prepare_specific_graph_model!")

    def compute_mean_and_logvariance(self):
        raise Exception("Models have to implement compute_mean_and_logvariance!")

    def sample_with_mean_and_logvariance(self):
        raise Exception("Models have to implement sample_with_mean_and_logvariance!")

    def construct_nodes(self):
        raise Exception("Models have to implement construct_molecules!")

    def construct_logit_matrices(self):
        raise Exception("Models have to implement construct_logit_matrices!")

    def construct_loss(self):
        raise Exception("Models have to implement construct_loss!")

    def make_minibatch_iterator(self, data: Any, is_training: bool):
        raise Exception("Models have to implement make_minibatch_iterator!")

    def run_epoch(self, epoch_name: str, epoch_num, data, is_training: bool):
        print(
            "Starting %s epoch %i, is_training %s"
            % (epoch_name, epoch_num, str(is_training))
        )

        loss = 0
        mean_edge_loss = 0
        mean_node_loss = 0
        mean_kl_loss = 0
        mean_qed_loss = 0
        node_loss_error = -10000000
        node_pred_error = 0
        start_time = time.time()
        processed_graphs = 0
        if is_training and self.params["num_teacher_forcing"] >= epoch_num:
            teacher_forcing = True
        else:
            teacher_forcing = False
        batch_iterator = ThreadedIterator(
            self.make_minibatch_iterator(data, is_training),
            max_queue_size=self.params["batch_size"],
        )  # self.params['batch_size'])

        for step, batch_data in enumerate(batch_iterator):
            num_graphs = batch_data[self.placeholders["num_graphs"]]
            processed_graphs += num_graphs
            batch_data[self.placeholders["is_generative"]] = False
            batch_data[self.placeholders["use_teacher_forcing_nodes"]] = teacher_forcing
            batch_data[self.placeholders["z_prior"]] = utils.generate_std_normal(
                self.params["batch_size"],
                batch_data[self.placeholders["num_vertices"]],
                self.params["hidden_size_encoder"],
            )

            if is_training:
                batch_data[
                    self.placeholders["out_layer_dropout_keep_prob"]
                ] = self.params["out_layer_dropout_keep_prob"]
                fetch_list = [
                    self.ops["loss"],
                    self.ops["train_step"],
                    self.ops["edge_loss"],
                    self.ops["kl_loss"],
                    self.ops["node_symbol_prob"],
                    self.placeholders["node_symbols"],
                    self.ops["qed_computed_values"],
                    self.placeholders["target_values"],
                    self.ops["total_qed_loss"],
                    self.ops["mean"],
                    self.ops["logvariance"],
                    self.ops["grads"],
                    self.ops["mean_edge_loss"],
                    self.ops["mean_node_symbol_loss"],
                    self.ops["mean_kl_loss"],
                    self.ops["mean_total_qed_loss"],
                    self.ops["grads2"],
                    self.ops["node_loss_error"],
                    self.ops["node_pred_error"],
                ]
            else:
                batch_data[self.placeholders["out_layer_dropout_keep_prob"]] = 1.0
                fetch_list = [
                    self.ops["loss"],
                    self.ops["mean_edge_loss"],
                    self.ops["mean_node_symbol_loss"],
                    self.ops["mean_kl_loss"],
                    self.ops["mean_total_qed_loss"],
                    self.ops["sampled_atoms"],
                    self.ops["node_loss_error"],
                    self.ops["node_pred_error"],
                ]
            result = self.sess.run(fetch_list, feed_dict=batch_data)
            batch_loss = result[0]
            loss += batch_loss * num_graphs
            if is_training:
                mean_edge_loss += result[12] * num_graphs
                mean_node_loss += result[13] * num_graphs
                mean_kl_loss += result[14] * num_graphs
                mean_qed_loss += result[15] * num_graphs
                node_loss_error = max(node_loss_error, np.max(result[17]))
                node_pred_error += result[18]
            else:
                mean_edge_loss += result[1] * num_graphs
                mean_node_loss += result[2] * num_graphs
                mean_kl_loss += result[3] * num_graphs
                mean_qed_loss += result[4] * num_graphs
                node_loss_error = max(node_loss_error, np.max(result[6]))
                node_pred_error += result[7]

            print(
                "Running %s, batch %i (has %i graphs). Total loss: %.4f. Edge loss: %.4f. Node loss: %.4f. KL loss: %.4f. Property loss: %.4f. Node error: %.4f. Node pred: %.4f. processed_graphs: %i"
                % (
                    epoch_name,
                    step,
                    num_graphs,
                    loss / processed_graphs,
                    mean_edge_loss / processed_graphs,
                    mean_node_loss / processed_graphs,
                    mean_kl_loss / processed_graphs,
                    mean_qed_loss / processed_graphs,
                    node_loss_error,
                    node_pred_error / processed_graphs,
                    processed_graphs,
                ),
                end="\n",
            )

        print(processed_graphs, "processed_graphs")
        mean_edge_loss /= processed_graphs
        mean_node_loss /= processed_graphs
        mean_kl_loss /= processed_graphs
        mean_qed_loss /= processed_graphs
        loss = loss / processed_graphs
        instance_per_sec = processed_graphs / (time.time() - start_time)

        return (
            loss,
            mean_edge_loss,
            mean_node_loss,
            mean_kl_loss,
            mean_qed_loss,
            instance_per_sec,
        )

    def generate_new_graphs(self, data):
        raise Exception("Models have to implement generate_new_graphs!")

    def reconstruction(self, data):
        raise Exception("Models have to implement generate_new_graphs!")

    def train(self):
        if self.params["generation"] == 0:
            print("START TRAINING")
        elif self.params["generation"] == 1:
            print("START GENERATION")
        elif self.params["generation"] == 2:
            print("START RECONSTRUCTION")
        elif self.params["generation"] == 3:
            print("START TEST")
        suff = "_" + self.params["suffix"] if self.params["suffix"] is not None else ""
        log_to_save = []
        total_time_start = time.time()
        with self.graph.as_default():
            for epoch in range(1, self.params["num_epochs"] + 1):
                if self.params["generation"] == 0:
                    print("========== EPOCH %i =================" % epoch)

                    (
                        loss,
                        mean_edge_loss,
                        mean_node_loss,
                        mean_kl_loss,
                        mean_qed_loss,
                        instance_per_sec,
                    ) = self.run_epoch(
                        "epoch %i (training)" % epoch, epoch, self.train_data, True
                    )

                    print(
                        "\n\x1b[K Train loss: %.5f | Edge loss: %.5f | Node loss: %.5f | KL loss: %.5f | QED loss: %.5f | instances/sec: %.2f"
                        % (
                            loss,
                            mean_edge_loss,
                            mean_node_loss,
                            mean_kl_loss,
                            mean_qed_loss,
                            instance_per_sec,
                        )
                    )

                    (
                        loss,
                        mean_edge_loss,
                        mean_node_loss,
                        mean_kl_loss,
                        mean_qed_loss,
                        instance_per_sec,
                    ) = self.run_epoch(
                        "epoch %i (validation)" % epoch, epoch, self.valid_data, False
                    )

                    print(
                        "\n\x1b[K Valid loss: %.5f | Edge loss: %.5f | Node loss: %.5f | KL loss: %.5f | QED loss: %.5f | instances/sec: %.2f"
                        % (
                            loss,
                            mean_edge_loss,
                            mean_node_loss,
                            mean_kl_loss,
                            mean_qed_loss,
                            instance_per_sec,
                        )
                    )

                    epoch_time = time.time() - total_time_start
                    log_entry = {
                        "Epoch": epoch,
                        "Time": epoch_time,
                        "Train_results": (
                            loss,
                            mean_edge_loss,
                            mean_node_loss,
                            mean_kl_loss,
                            mean_qed_loss,
                            instance_per_sec,
                        ),
                    }
                    log_to_save.append(log_entry)

                    with open(self.log_file, "w") as f:
                        json.dump(log_to_save, f, indent=4)

                    self.save_model(
                        "%s%s.pickle" % (safe_dataset(self.params["dataset"]), suff)
                    )

                    print("Generating new graphs")
                    self.generate_new_graphs(self.train_data)

                elif self.params["generation"] == 1:
                    self.generate_new_graphs(self.train_data)
                elif self.params["generation"] == 2:
                    self.reconstruction(self.test_data)
                elif self.params["generation"] == 3:  # validation only
                    (
                        loss,
                        mean_edge_loss,
                        mean_node_loss,
                        mean_kl_loss,
                        mean_qed_loss,
                        instance_per_sec,
                    ) = self.run_epoch(
                        "epoch %i (training)" % epoch, epoch, self.train_data, False
                    )

                    print(
                        "\n\x1b[K Train loss: %.5f | Edge loss: %.5f | Node loss: %.5f | KL loss: %.5f | QED loss: %.5f | instances/sec: %.2f"
                        % (
                            loss,
                            mean_edge_loss,
                            mean_node_loss,
                            mean_kl_loss,
                            mean_qed_loss,
                            instance_per_sec,
                        )
                    )

                    (
                        loss,
                        mean_edge_loss,
                        mean_node_loss,
                        mean_kl_loss,
                        mean_qed_loss,
                        instance_per_sec,
                    ) = self.run_epoch(
                        "epoch %i (valid)" % epoch, epoch, self.valid_data, False
                    )

                    print(
                        "\n\x1b[K Valid loss: %.5f | Edge loss: %.5f | Node loss: %.5f | KL loss: %.5f | QED loss: %.5f | instances/sec: %.2f"
                        % (
                            loss,
                            mean_edge_loss,
                            mean_node_loss,
                            mean_kl_loss,
                            mean_qed_loss,
                            instance_per_sec,
                        )
                    )

                    (
                        loss,
                        mean_edge_loss,
                        mean_node_loss,
                        mean_kl_loss,
                        mean_qed_loss,
                        instance_per_sec,
                    ) = self.run_epoch(
                        "epoch %i (test)" % epoch, epoch, self.test_data, False
                    )

                    print(
                        "\n\x1b[K Test loss: %.5f | Edge loss: %.5f | Node loss: %.5f | KL loss: %.5f | QED loss: %.5f | instances/sec: %.2f"
                        % (
                            loss,
                            mean_edge_loss,
                            mean_node_loss,
                            mean_kl_loss,
                            mean_qed_loss,
                            instance_per_sec,
                        )
                    )
                    exit(0)

    def save_model(self, path: str) -> None:
        weights_to_save = {}
        for variable in self.sess.graph.get_collection(tf.GraphKeys.GLOBAL_VARIABLES):
            assert variable.name not in weights_to_save
            weights_to_save[variable.name] = self.sess.run(variable)

        data_to_save = {"params": self.params, "weights": weights_to_save}

        log_dir = self.params["log_dir"]
        with open(log_dir + "/" + path, "wb") as out_file:
            pickle.dump(data_to_save, out_file, pickle.HIGHEST_PROTOCOL)

    def initialize_model(self) -> None:
        init_op = tf.group(
            tf.global_variables_initializer(), tf.local_variables_initializer()
        )
        self.sess.run(init_op)

    def restore_model(self, path: str) -> None:
        print("Restoring weights from file %s." % path)
        with open(path, "rb") as in_file:
            data_to_load = pickle.load(in_file)

        variables_to_initialize = []
        with tf.name_scope("restore"):
            restore_ops = []
            used_vars = set()
            for variable in self.sess.graph.get_collection(
                tf.GraphKeys.GLOBAL_VARIABLES
            ):
                used_vars.add(variable.name)
                if variable.name in data_to_load["weights"]:
                    restore_ops.append(
                        variable.assign(data_to_load["weights"][variable.name])
                    )
                else:
                    print(
                        "Freshly initializing %s since no saved value was found."
                        % variable.name
                    )
                    variables_to_initialize.append(variable)
            for var_name in data_to_load["weights"]:
                if var_name not in used_vars:
                    print("Saved weights for %s not used by model." % var_name)
            restore_ops.append(tf.variables_initializer(variables_to_initialize))
            self.sess.run(restore_ops)

    def get_time_diff(self, t_new, t_old):
        diff = t_new - t_old
        h = diff // (60 * 60)
        rim = diff % (60 * 60)
        m = rim // 60
        s = rim % 60
        return "H: " + str(h) + "   M: " + str(m) + "   S: " + str(round(s, 1))
