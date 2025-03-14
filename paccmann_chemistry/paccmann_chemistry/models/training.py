"""Train and Test Functions and Utilities."""
import json
import os
from time import time

import numpy as np
import torch

from ..utils import (
    crop_start_stop,
    get_device,
    packed_sequential_data_preparation,
    print_example_reconstruction,
    sequential_data_preparation,
    unpack_sequence,
)
from ..utils.hyperparams import OPTIMIZER_FACTORY
from ..utils.loss_functions import vae_loss_function
from ..utils.search import SamplingSearch
from rdkit import Chem
from rdkit.Chem import Draw

GENERATED_MOLECULES = 0


def test_vae(model, dataloader, logger, input_keep, batch_mode):
    """
    VAE test function.

    Args:
        model: Model object to be tested.
        dataloader (DataLoader): DataLoader object returning test data batches.
        logger (logging.Logger): To display information on the fly.
        input_keep (float): The probability not to drop input sequence tokens
            according to a Bernoulli distribution with p = input_keep.

    Returns:
        float: average test loss over the entire test data.
    """
    device = get_device()
    data_preparation = get_data_preparation(batch_mode)
    vae_model = model.to(device)
    vae_model.eval()
    test_loss, test_rec, test_kl_div = 0, 0, 0
    with torch.no_grad():
        for _iter, batch in enumerate(dataloader):
            if (_iter + 1) % 500 == 0:
                logger.info(f"**TESTING**\t Processing batch {_iter}/{len(dataloader)}")

            (encoder_seq, decoder_seq, target_seq) = data_preparation(
                batch, input_keep=input_keep, start_index=2, end_index=3, device=device
            )

            decoder_loss, mu, logvar = vae_model(encoder_seq, decoder_seq, target_seq)
            loss, kl_div = vae_loss_function(decoder_loss, mu, logvar, eval_mode=True)
            test_loss += loss.item()
            test_rec += decoder_loss.item()
            test_kl_div += kl_div.item()
    test_loss /= len(dataloader)
    test_rec /= len(dataloader)
    test_kl_div /= len(dataloader)
    vae_model.train()
    return test_loss, test_rec, test_kl_div


def train_vae(
    epoch,
    model,
    train_dataloader,
    val_dataloader,
    smiles_language,
    model_dir,
    search=SamplingSearch(),
    optimizer="adam",
    lr=1e-3,
    kl_growth=0.0015,
    input_keep=1.0,
    test_input_keep=0.0,
    start_index=2,
    end_index=3,
    generate_len=100,
    log_interval=100,
    eval_interval=200,
    save_interval=200,
    loss_tracker=None,
    logger=None,
    batch_mode="padded",
    writer=None,
    total_epochs=-1,
):  # yapf: disable
    """
    VAE train function.

    Args:
        epoch (int): Epoch number.
        model: Model object to train.
        train_dataloader (DataLoader): DataLoader object returning
            training batches.
        val_dataloader (DataLoader): DataLoader object returning
            validation batches.
        smiles_language (SMILESLanguage): SMILESLanguage object.
        model_dir (str): The path to the directory where model will
            be saved.
        search (paccmann_chemistry.utils.search.Search): search strategy
                used in the decoder.
        optimizer (str): Choice from OPTIMIZER_FACTORY. Defaults to 'adam'.
        lr (float): The learning rate.
        kl_growth (float): The rate at which the weight grows.
            Defaults to 0.0015 resulting in a weight of 1 around step=9000.
        input_keep (float): The probability not to drop input sequence tokens
            according to a Bernoulli distribution with p = input_keep.
            Defaults to 1.
        test_input_keep (float): Like the input_keep parameter, but for
            test. Defaults to 0.
        generate_len (int): Length of the generated molecule.
        log_interval (int): The interval at which average loss is
            recorded.
        eval_interval (int): The interval at which a molecule is generated
            and displayed.
        save_interval (int): The interval at which the model is saved.
        loss_tracker (dict): At each log_interval, update improved test
            losses and respective epoch.
        logger (logging.Logger): To display information on the fly.
        batch_mode (str): Batch mode to use.

    Returns:
         dict: updated loss_tracker.
    """
    global GENERATED_MOLECULES
    if loss_tracker is None:
        loss_tracker = {
            "test_loss_a": 10e4,
            "test_rec_a": 10e4,
            "test_kld_a": 10e4,
            "ep_loss": 0,
            "ep_rec": 0,
            "ep_kld": 0,
        }

    device = get_device()
    selfies = smiles_language.selfies
    data_preparation = get_data_preparation(batch_mode)
    vae_model = model.to(device)
    vae_model.train()
    train_loss = 0
    optimizer = OPTIMIZER_FACTORY[optimizer](vae_model.parameters(), lr=lr)
    t = time()
    for _iter, batch in enumerate(train_dataloader):
        global_step = epoch * len(train_dataloader) + _iter

        encoder_seq, decoder_seq, target_seq = data_preparation(
            batch,
            input_keep=input_keep,
            start_index=start_index,
            end_index=end_index,
            device=device,
        )

        optimizer.zero_grad()
        decoder_loss, mu, logvar = vae_model(encoder_seq, decoder_seq, target_seq)
        loss, kl_div = vae_loss_function(
            decoder_loss, mu, logvar, kl_growth=kl_growth, step=global_step
        )
        loss.backward()
        train_loss += loss.detach().item()

        optimizer.step()
        torch.cuda.empty_cache()

        if writer:
            writer.add_scalar("train/loss", loss.item(), global_step=global_step)
            writer.add_scalar("train/loss_dec", loss.item(), global_step=global_step)
            writer.add_scalar("train/kl_div", loss.item(), global_step=global_step)

        if _iter and _iter % log_interval == 0:
            logger.info(
                f"***TRAINING***\t Epoch: {epoch}, "
                f"step {_iter}/{len(train_dataloader)}.\t"
                f"Loss: {train_loss/log_interval:2.4f}, time spent: {time()-t}"
            )
            t = time()
            train_loss = 0

            if batch_mode == "packed":
                target_seq = unpack_sequence(target_seq)

            # target, pred = print_example_reconstruction(
            #     vae_model.decoder.outputs, target_seq, smiles_language, selfies
            # )
            # if writer:
            #     writer.add_text(
            #         "mol/train/reconstructed",
            #         f"Sample\t{target}\nReconstr:\t{pred}",
            #         global_step=global_step,
            #     )
            #     mol = Chem.MolFromSmiles(target)
            #     molt = Chem.MolFromSmiles(pred)
            #     if mol and molt:
            #         writer.add_image(
            #             "mol/train/reconstructed",
            #             np.array(Draw.MolsToImage([mol, molt])),
            #             dataformats="HWC",
            #             global_step=global_step,
            #         )

            # logger.info(
            #     (f"Sample input:\n\t {target}, " f"model reconstructed:\n\t {pred}")
            # )

        if _iter and _iter % save_interval == 0:
            save_dir = os.path.join(
                model_dir, f"weights/saved_model_epoch_{epoch}_iter_{_iter}.pt"
            )
            vae_model.save(save_dir)
            logger.info(f"***SAVING***\t Epoch {epoch}, saved model.")
        is_final = epoch == total_epochs
        if is_final or (epoch and epoch % eval_interval == 0):
            vae_model.eval()
            latent_z = torch.randn(1, mu.shape[0], mu.shape[1]).to(device)
            molecule_iter = vae_model.generate(
                latent_z,
                prime_input=torch.tensor(
                    [train_dataloader.dataset.smiles_language.start_index]
                ).to(device),
                end_token=torch.tensor(
                    [train_dataloader.dataset.smiles_language.stop_index]
                ).to(device),
                generate_len=generate_len if not is_final else GENERATED_MOLECULES,
                search=search,
            )
            with open(
                f"{model_dir}/generated_molecules.{'in-training.txt' if not is_final else 'the-end'}",
                "a",
            ) as f:
                for mol in molecule_iter:
                    GENERATED_MOLECULES += 1
                    mol = smiles_language.token_indexes_to_smiles(
                        crop_start_stop(mol, smiles_language)
                    )
                    # SELFIES conversion if necessary
                    mol = smiles_language.selfies_to_smiles(mol) if selfies else mol
                    mol = mol.strip()
                    if mol:
                        logger.info(f"\nSample Generated Molecule:\n{mol}")
                        f.write(f"{mol}\n")

            # if writer:
            #     writer.add_text("mol/test/generated", f"{mol}", global_step=global_step)
            #     mol = Chem.MolFromSmiles(mol)
            #     if mol:
            #         writer.add_image(
            #             "mol/test/generated",
            #             np.array(Draw.MolsToImage([mol])),
            #             dataformats="HWC",
            #             global_step=global_step,
            #         )
            # target, pred = print_example_reconstruction(
            #     vae_model.decoder.outputs, target_seq, smiles_language, selfies
            # )
            # if writer:
            #     writer.add_text(
            #         "mol/test/reconstructed",
            #         f"Sample\t{target}\nReconstr:\t{pred}",
            #         global_step=global_step,
            #     )
            #     mol = Chem.MolFromSmiles(target)
            #     molt = Chem.MolFromSmiles(pred)
            #     if mol and molt:
            #         writer.add_image(
            #             "mol/test/reconstructed",
            #             np.array(Draw.MolsToImage([mol, molt])),
            #             dataformats="HWC",
            #             global_step=global_step,
            #         )

            # test_loss, test_rec, test_kld = test_vae(
            #     vae_model, val_dataloader, logger, test_input_keep, batch_mode
            # )
            # logger.info(
            #     f"***TESTING*** \t Epoch {epoch}, test loss = "
            #     f"{test_loss:.4f}, reconstruction = {test_rec:.4f}, "
            #     f"KL = {test_kld:.4f}."
            # )
            vae_model.train()
            # if writer:
            #     writer.add_scalar("test/loss", test_loss, global_step=global_step)
            #     writer.add_scalar("test/loss_dec", test_rec, global_step=global_step)
            #     writer.add_scalar("test/kl_div", test_kld, global_step=global_step)
            # if test_loss < loss_tracker["test_loss_a"]:
            #     loss_tracker.update({"test_loss_a": test_loss, "ep_loss": epoch})
            #     vae_model.save(os.path.join(model_dir, f"weights/best_loss.pt"))
            #     logger.info(
            #         f"Epoch {epoch}. NEW best test loss = {test_loss:.4f} \t"
            #         f"(Rec = {test_rec:.4f}, KLD = {test_kld:.4f})."
            #     )

            # if test_rec < loss_tracker["test_rec_a"]:
            #     loss_tracker.update({"test_rec_a": test_rec, "ep_rec": epoch})
            #     vae_model.save(os.path.join(model_dir, f"weights/best_rec.pt"))
            #     logger.info(
            #         f"Epoch {epoch}. NEW best reconstruction loss = "
            #         f"{test_rec:.4f} \t (Loss = {test_loss:.4f}, KLD = "
            #         f"{test_kld:.4f})"
            #     )
            # if test_kld < loss_tracker["test_kld_a"]:
            #     loss_tracker.update({"test_kld_a": test_kld, "ep_kld": epoch})
            #     vae_model.save(os.path.join(model_dir, f"weights/best_kld.pt"))
            #     logger.info(
            #         f"Epoch {epoch}. NEW best KLD = {test_kld:.4f} \t (loss "
            #         f"= {test_loss:.4f}, Reconstruction = {test_rec:.4f})."
            #     )
            # with open(os.path.join(model_dir, "loss_tracker.json"), "w") as fp:
            #     json.dump(loss_tracker, fp)

    logger.info(
        f"Epoch {epoch} finished, \t Training Loss = {loss.item():.4f},"
        f"Reconstruction = {decoder_loss.item():.4f}, KL = "
        f"{(loss - decoder_loss).item():.8f}"
    )

    return loss_tracker


def _prepare_packed(batch, input_keep, start_index, end_index, device=None):
    encoder_seq, decoder_seq, target_seq = packed_sequential_data_preparation(
        batch, input_keep=input_keep, start_index=start_index, end_index=end_index
    )

    return encoder_seq, decoder_seq, target_seq


def _prepare_padded(batch, input_keep, start_index, end_index, device):
    padded_batch = torch.nn.utils.rnn.pad_sequence(batch)
    padded_batch = padded_batch.to(device)
    encoder_seq, decoder_seq, target_seq = sequential_data_preparation(
        padded_batch,
        input_keep=input_keep,
        start_index=start_index,
        end_index=end_index,
    )
    return encoder_seq, decoder_seq, target_seq


def get_data_preparation(mode):
    """Select data preparation function mode

    Args:
        mode (str): Mode to use. Available modes:
            `Padded`, `Packed`

    """
    if not isinstance(mode, str):
        raise TypeError("Argument `mode` should be a string.")
    mode = mode.capitalize()
    MODES = {"Padded": _prepare_padded, "Packed": _prepare_packed}
    if mode not in MODES:
        raise NotImplementedError(
            f"Unknown mode: {mode}. Available modes: {MODES.keys()}"
        )
    return MODES[mode]
