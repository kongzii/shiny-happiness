#!/usr/bin/env python3
"""Train TeacherVAE molecule generator."""
import argparse
import json
import logging
import os
import sys
from time import time
from paccmann_chemistry.utils import collate_fn, get_device, disable_rdkit_logging
from paccmann_chemistry.models.vae import StackGRUDecoder, StackGRUEncoder, TeacherVAE
from paccmann_chemistry.models.training import train_vae
from paccmann_chemistry.utils.hyperparams import SEARCH_FACTORY
from pytoda.datasets import SMILESDataset, SMILESTokenizerDataset
from pytoda.smiles.smiles_language import SMILESLanguage
from torch.utils.tensorboard import SummaryWriter
import torch

# setup logging
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
logger = logging.getLogger("training_vae")

# yapf: disable
parser = argparse.ArgumentParser(description='Chemistry VAE training script.')
parser.add_argument(
    'train_smiles_filepath', type=str,
    help='Path to the train data file (.smi).'
)
parser.add_argument(
    'test_smiles_filepath', type=str,
    help='Path to the test data file (.smi).'
)
parser.add_argument(
    'smiles_language_filepath', type=str,
    help='Path to SMILES language object.'
)
parser.add_argument(
    'model_path', type=str,
    help='Directory where the model will be stored.'
)
parser.add_argument(
    'params_filepath', type=str,
    help='Path to the parameter file.'
)
parser.add_argument(
    'training_name', type=str,
    help='Name for the training.'
)
# yapf: enable


def main(parser_namespace):
    try:
        device = get_device()
        disable_rdkit_logging()
        # read the params json
        params = dict()
        with open(parser_namespace.params_filepath) as f:
            params.update(json.load(f))

        # get params
        train_smiles_filepath = parser_namespace.train_smiles_filepath
        test_smiles_filepath = parser_namespace.test_smiles_filepath
        smiles_language_filepath = (
            parser_namespace.smiles_language_filepath
            if parser_namespace.smiles_language_filepath.lower() != "none"
            else None
        )

        model_path = parser_namespace.model_path
        training_name = parser_namespace.training_name

        writer = SummaryWriter(f"logs/{training_name}")

        logger.info(f"Model with name {training_name} starts.")

        model_dir = os.path.join(model_path, training_name)
        log_path = os.path.join(model_dir, "logs")
        val_dir = os.path.join(log_path, "val_logs")
        os.makedirs(os.path.join(model_dir, "weights"), exist_ok=True)
        os.makedirs(os.path.join(model_dir, "results"), exist_ok=True)
        os.makedirs(log_path, exist_ok=True)
        os.makedirs(val_dir, exist_ok=True)

        # Load SMILES language
        smiles_language = None
        if smiles_language_filepath is not None:
            smiles_language = SMILESLanguage.load(smiles_language_filepath)

        logger.info(f"Smiles filepath: {train_smiles_filepath}")

        # create SMILES eager dataset
        smiles_train_data_with_lang = SMILESTokenizerDataset(
            train_smiles_filepath,
            smiles_language=smiles_language,
            padding=False,
            selfies=params.get("selfies", False),
            add_start_and_stop=params.get("add_start_stop_token", True),
            augment=params.get("augment_smiles", False),
            canonical=params.get("canonical", False),
            kekulize=params.get("kekulize", False),
            all_bonds_explicit=params.get("all_bonds_explicit", False),
            all_hs_explicit=params.get("all_hs_explicit", False),
            remove_bonddir=params.get("remove_bonddir", False),
            remove_chirality=params.get("remove_chirality", False),
            backend="eager",
            device=device,
        )
        if smiles_language_filepath is None:
            smiles_language = smiles_train_data_with_lang.smiles_language
            smiles_language.save(os.path.join(model_path, f"{training_name}.lang"))
        else:
            smiles_language_filename = os.path.basename(smiles_language_filepath)
            smiles_language.save(os.path.join(model_dir, smiles_language_filename))
        smiles_train_data = SMILESTokenizerDataset(
            train_smiles_filepath,
            smiles_language=smiles_language,
            padding=False,
            selfies=params.get("selfies", False),
            add_start_and_stop=params.get("add_start_stop_token", True),
            augment=params.get("augment_smiles", False),
            canonical=params.get("canonical", False),
            kekulize=params.get("kekulize", False),
            all_bonds_explicit=params.get("all_bonds_explicit", False),
            all_hs_explicit=params.get("all_hs_explicit", False),
            remove_bonddir=params.get("remove_bonddir", False),
            remove_chirality=params.get("remove_chirality", False),
            backend="eager",
            device=device,
        )
        smiles_test_data = SMILESTokenizerDataset(
            test_smiles_filepath,
            smiles_language=smiles_language,
            padding=False,
            selfies=params.get("selfies", False),
            add_start_and_stop=params.get("add_start_stop_token", True),
            augment=params.get("augment_smiles", False),
            canonical=params.get("canonical", False),
            kekulize=params.get("kekulize", False),
            all_bonds_explicit=params.get("all_bonds_explicit", False),
            all_hs_explicit=params.get("all_hs_explicit", False),
            remove_bonddir=params.get("remove_bonddir", False),
            remove_chirality=params.get("remove_chirality", False),
            backend="eager",
            device=device,
        )

        params.update(
            {
                "vocab_size": smiles_language.number_of_tokens,
                "pad_index": smiles_language.padding_index,
            }
        )

        vocab_dict = smiles_language.index_to_token
        params.update(
            {
                "start_index": list(vocab_dict.keys())[
                    list(vocab_dict.values()).index("<START>")
                ],
                "end_index": list(vocab_dict.keys())[
                    list(vocab_dict.values()).index("<STOP>")
                ],
            }
        )

        if params.get("embedding", "learned") == "one_hot":
            params.update({"embedding_size": params["vocab_size"]})

        with open(os.path.join(model_dir, "model_params.json"), "w") as fp:
            json.dump(params, fp)

        # create DataLoaders
        train_data_loader = torch.utils.data.DataLoader(
            smiles_train_data,
            batch_size=params.get("batch_size", 64),
            collate_fn=collate_fn,
            drop_last=True,
            shuffle=True,
            pin_memory=params.get("pin_memory", True),
            num_workers=params.get("num_workers", 8),
        )

        test_data_loader = torch.utils.data.DataLoader(
            smiles_test_data,
            batch_size=params.get("batch_size", 64),
            collate_fn=collate_fn,
            drop_last=True,
            shuffle=True,
            pin_memory=params.get("pin_memory", True),
            num_workers=params.get("num_workers", 8),
        )
        # initialize encoder and decoder
        gru_encoder = StackGRUEncoder(params).to(device)
        gru_decoder = StackGRUDecoder(params).to(device)
        gru_vae = TeacherVAE(gru_encoder, gru_decoder).to(device)
        # TODO I haven't managed to get this to work. I will leave it here
        # if somewant (or future me) wants to give it a look and get the
        # tensorboard graph to work
        # if writer and False:
        #     gru_vae.set_batch_mode('padded')
        #     dummy_input = torch.ones(smiles_train_data[0].shape)
        #     dummy_input = dummy_input.unsqueeze(0).to(device)
        #     writer.add_graph(gru_vae, (dummy_input, dummy_input, dummy_input))
        #     gru_vae.set_batch_mode(params.get('batch_mode'))
        logger.info("\n****MODEL SUMMARY***\n")
        for name, parameter in gru_vae.named_parameters():
            logger.info(f"Param {name}, shape:\t{parameter.shape}")
        total_params = sum(p.numel() for p in gru_vae.parameters())
        logger.info(f"Total # params: {total_params}")

        loss_tracker = {
            "test_loss_a": 10e4,
            "test_rec_a": 10e4,
            "test_kld_a": 10e4,
            "ep_loss": 0,
            "ep_rec": 0,
            "ep_kld": 0,
        }

        # train for n_epoch epochs
        logger.info("Model creation and data processing done, Training starts.")
        decoder_search = SEARCH_FACTORY[params.get("decoder_search", "sampling")](
            temperature=params.get("temperature", 1.0),
            beam_width=params.get("beam_width", 3),
            top_tokens=params.get("top_tokens", 5),
        )  # yapf: disable

        if writer:
            pparams = params.copy()
            pparams["training_file"] = train_smiles_filepath
            pparams["test_file"] = test_smiles_filepath
            pparams["language_file"] = smiles_language_filepath
            pparams["model_path"] = model_path
            pparams = {k: v if v is not None else "N.A." for k, v in params.items()}
            pparams["training_name"] = training_name
            from pprint import pprint

            pprint(pparams)
            writer.add_hparams(hparam_dict=pparams, metric_dict={})

        for epoch in range(params["epochs"] + 1):
            t = time()
            loss_tracker = train_vae(
                epoch,
                gru_vae,
                train_data_loader,
                test_data_loader,
                smiles_language,
                model_dir,
                search=decoder_search,
                optimizer=params.get("optimizer", "adadelta"),
                lr=params["learning_rate"],
                kl_growth=params["kl_growth"],
                input_keep=params["input_keep"],
                test_input_keep=params["test_input_keep"],
                generate_len=params["generate_len"],
                log_interval=params["log_interval"],
                save_interval=params["save_interval"],
                eval_interval=params["eval_interval"],
                loss_tracker=loss_tracker,
                logger=logger,
                # writer=writer,
                batch_mode=params.get("batch_mode"),
                total_epochs=params["epochs"],
            )
            logger.info(f"Epoch {epoch}, took {time() - t:.1f}.")

        logger.info(
            "OVERALL: \t Best loss = {0:.4f} in Ep {1}, "
            "best Rec = {2:.4f} in Ep {3}, "
            "best KLD = {4:.4f} in Ep {5}".format(
                loss_tracker["test_loss_a"],
                loss_tracker["ep_loss"],
                loss_tracker["test_rec_a"],
                loss_tracker["ep_rec"],
                loss_tracker["test_kld_a"],
                loss_tracker["ep_kld"],
            )
        )
        logger.info("Training done, shutting down.")
    except Exception:
        logger.exception("Exception occurred while running train_vae.py.")


def framerize(path):
    with open(path) as f:
        lines = f.readlines()

    new_path = f"{path}.framed"
    with open(new_path, "w") as f:
        # f.write("SMILES\tindex\n")
        for idx, line in enumerate(lines):
            f.write(f"{line.strip()}\t{idx}\n")

    return new_path


if __name__ == "__main__":
    args = parser.parse_args()
    args.train_smiles_filepath = framerize(args.train_smiles_filepath)
    args.test_smiles_filepath = framerize(args.test_smiles_filepath)
    main(parser_namespace=args)
