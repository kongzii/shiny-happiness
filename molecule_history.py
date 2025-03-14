import re
import typer
import pandas as pd
import matplotlib.pyplot as plt
import itertools as it
from typing import Union
from rdkit import Chem
from tqdm import tqdm
from math import ceil, sqrt
from pathlib import Path
from collections import Counter, defaultdict
from compare_runs import get_clear_val_canon_smiles

plt.rcParams["text.usetex"] = True


def main(log: bool = False):
    Path("report").mkdir(exist_ok=True, parents=True)
    all_generated_files = (
        list(Path(".").rglob("*generated_smiles.txt"))
        + list(Path(".").rglob("*generated_samples.txt"))
        + list(Path(".").rglob("*generated_molecules.txt"))
    )

    for max_num_atoms in tqdm(
        [
            8,
            12,
            15,
            20,
            25,
            30,
        ],
        desc="Plotting molecule history",
    ):
        filtered_files = [
            x
            for x in all_generated_files
            if not str(x).startswith(".backup")
            and ("in-training" in str(x) or "the-end" in str(x))
            and (
                f"{max_num_atoms}_generated_smiles.txt" in str(x)
                or f"_{max_num_atoms}/generated_samples.txt" in str(x)
                or f"_{max_num_atoms}/generated_smiles.txt" in str(x)
                or f"_{max_num_atoms}/generated_molecules.txt" in str(x)
                or f"_{max_num_atoms}_" in str(x)
            )
        ]
        if not filtered_files:
            print("No files for ", max_num_atoms)
            continue
        plot_history(max_num_atoms, filtered_files, log)


def load_smiles_as_canon(max_num_atoms: int) -> list:
    smiles = []
    for line in open(f"/app/data/molecules/size_{max_num_atoms}/valid.smi"):
        # mol = Chem.MolFromSmiles(line.strip())
        canon = Chem.CanonSmiles(line)
        smiles.append(canon)
    return smiles


def convert_smiles_to_canon(smiles: list) -> list:
    converted = []
    errored = 0
    for smile in smiles:
        try:
            canon = Chem.CanonSmiles(smile)
        except:
            errored += 1
            print("Can not convert", smile)
            continue
        converted.append(canon)
    if errored:
        print("Errored", errored)
    return converted


def plot_history(
    max_num_atoms: int,
    files: list,
    log: bool,
):
    print(f"Plotting history for {max_num_atoms} atoms")
    val_canons = load_smiles_as_canon(max_num_atoms)
    val_canons_set = set(val_canons)

    group_to_methods = {
        "everything": (
            "DiGress discrete",
            "DiGress continuous",
            "Data Efficient Grammar",
            "MoLeR",
            "Paccmann VAE",
            "RNN Selfies",
            "RNN Regex",
            "RNN Char",
        ),
    }

    group_to_fig_axe = {
        key: plt.subplots(1, 3, figsize=(12 * 3, 12)) for key in group_to_methods.keys()
    }

    for _, axes in group_to_fig_axe.values():
        for ax in axes:
            ax.set_ylim([0, 1])

    molecules_counters = []
    methods = set()

    method_to_linestyle = {
        "DiGress discrete": ("-", "tab:cyan"),
        "DiGress continuous": ("-", "orange"),
        "Data Efficient Grammar": ("-", "tab:olive"),
        "MoLeR": ("-", "tab:blue"),
        "RNN Selfies": ("-", "wheat"),
        "RNN Regex": ("-", "aqua"),
        "RNN Char": ("-", "tab:red"),
        "Paccmann VAE": ("-", "tab:green"),
        # f"ds-q-{max_num_atoms} sum-max": ("-.", "tab:purple"),
        # f"orig-{max_num_atoms}": (":", "tab:brown"),
        # f"orig-seventh-depth-{max_num_atoms}": (":", "tab:pink"),
    }
    method_to_order = defaultdict(lambda: 1)

    # End of configs

    method_to_data = {}
    method_to_highest_generated = defaultdict(lambda: 0)
    allowed_methods = [m for methods_ in group_to_methods.values() for m in methods_]

    for file in files:
        method = filepath_to_title(file)
        print("Processing", method, file)
        methods.add(method)

        if method not in allowed_methods:
            print(method, " not in ", allowed_methods)
            continue

        style, color = method_to_linestyle.get(method, ("-", "gray"))

        history = get_molecule_history(file)
        if not history:
            raise RuntimeError("No history for ", method, file)
            continue
        history_dedup = dedup_by(history, lambda x: x)

        molecule_counter = Counter(history)
        molecules_counters.append((method, molecule_counter))

        generated_molecules_sorted = sorted(
            molecule_counter.keys(),
            key=lambda x: molecule_counter[x],
            reverse=True,
        )

        (
            sorted_val_sizes,
            sorted_val_recalls,
            sorted_val_precisions,
        ) = get_cumulative_perc_deduplicated(generated_molecules_sorted, val_canons)

        # If there are multiple same runs, take one that were running the longest (generated most molecules)
        if method_to_highest_generated[method] >= len(sorted_val_sizes):
            print("Warning - skipping", method, file)
            continue

        ranked_conf_matrix = get_conf_matrix(molecule_counter, val_canons)
        ranked_conf_matrix.to_csv(f"report/conf_matrix_{max_num_atoms}_{method}.csv")

        method_to_highest_generated[method] = len(sorted_val_sizes)
        method_to_data[method] = (
            method,
            style,
            color,
            history_dedup,
            sorted_val_sizes,
            sorted_val_recalls,
            sorted_val_precisions,
            " (in-training)" if "in-training" in str(file) else " (the-end)",
        )

    try:
        max_generated_number = max([len(data[3]) for data in method_to_data.values()])
    except ValueError:
        print("No data to plot for ", max_num_atoms)
        return
    unique_in_val_number = len(val_canons_set)

    for group, methods in group_to_methods.items():
        fig, axes = group_to_fig_axe[group]
        for method in methods:
            if method not in method_to_data:
                continue
            (
                method_2,
                style,
                color,
                history_dedup,
                sorted_val_sizes,
                sorted_val_recalls,
                sorted_val_precisions,
                type_,
            ) = method_to_data[method]
            assert method == method_2, (method, method_2)
            label = method + type_
            axes[0].set_xlabel(
                "Number of generated unique molecules sorted by frequency"
            )
            axes[0].set_ylabel("How many from validation data was generated (recall)")
            axes[0].plot(
                sorted_val_sizes[:max_generated_number],
                sorted_val_recalls[:max_generated_number],
                style,
                label=label,
                alpha=1.0,
                color=color,
            )
            axes[1].set_xlabel(
                "Number of generated unique molecules sorted by frequency"
            )
            axes[1].set_ylabel(
                "How many from generated molecules are in validation data (precision)"
            )
            axes[1].plot(
                sorted_val_sizes[:unique_in_val_number],
                sorted_val_precisions[:unique_in_val_number],
                style,
                label=label,
                alpha=1.0,
                color=color,
            )
            axes[2].set_xlabel("Recall")
            axes[2].set_ylabel("Precision")
            axes[2].set_xlim([0, 1])
            axes[2].plot(
                sorted_val_recalls[:unique_in_val_number],
                sorted_val_precisions[:unique_in_val_number],
                style,
                label=label,
                alpha=1.0,
                color=color,
            )

    for group, (fig, axes) in group_to_fig_axe.items():
        for ax in axes:
            ax.legend(loc="lower right")
            if log:
                ax.set_yscale("log")
            fig.savefig(f"report/molecule_history_{max_num_atoms}_{group}.jpg")


def get_conf_matrix(molecule_counter, val_canons) -> pd.DataFrame:
    val_canons_set = set(val_canons)
    counts = sorted(set(molecule_counter.values()), reverse=True)

    data = []

    for rank in counts:
        generated_molecules_above_rank = set(
            [m for m, c in molecule_counter.items() if c >= rank]
        )
        generated_molecules_below_rank = set(
            [m for m, c in molecule_counter.items() if c < rank]
        )

        tp = len(generated_molecules_above_rank & val_canons_set)
        fp = len(generated_molecules_above_rank - val_canons_set)
        tn = len(generated_molecules_below_rank - val_canons_set)
        fn = len(generated_molecules_below_rank & val_canons_set)

        data.append(
            {
                "len_val_canons_set": len(val_canons_set),
                "len_generated_molecules_above_rank": len(
                    generated_molecules_above_rank
                ),
                "len_generated_molecules_below_rank": len(
                    generated_molecules_below_rank
                ),
                "rank": rank,
                "tp": tp,
                "fp": fp,
                "tn": tn,
                "fn": fn,
            }
        )

    df = pd.DataFrame.from_records(data)
    return df


def get_cumulative_perc_deduplicated(gen_smiles, dataset_smiles):
    dataset_smiles_set = set(dataset_smiles)

    recalls = []
    precisions = []
    sizes = []
    subset = set()

    for molecule in tqdm(gen_smiles):
        subset.add(molecule)
        recalls.append(len(subset & dataset_smiles_set) / len(dataset_smiles_set))
        precisions.append(len(subset & dataset_smiles_set) / len(subset))
        sizes.append(len(subset))

    return sizes, recalls, precisions


def get_molecule_history(filepath: Union[str, Path]) -> list:
    history = []
    with open(filepath) as file:
        for idx, line in tqdm(enumerate(file), desc=f"Reading {filepath}"):
            line = line.strip()
            if not line:
                continue

            items = line.split()

            if "moler" in str(filepath) and idx < 20 and len(items) > 1:
                # Output made of >, not actual molecules.
                continue
            elif len(items) == 3:
                smile = items[2]
            elif len(items) == 2 and items[1] == "working":
                smile = items[0]
            elif len(items) == 1:
                smile = items[0]
            else:
                raise Exception(f"Unknown format, {items}")

            history.append(smile)

    return convert_smiles_to_canon(history)


def dedup_by(smiles: list, callable=lambda x: x) -> list:
    seen = set()
    deduped = []
    for smile in smiles:
        if callable(smile) not in seen:
            deduped.append(smile)
            seen.add(callable(smile))
    return deduped


def filepath_to_title(filepath: Path) -> str:
    filepath_str = str(filepath)

    if "DiGress" and "continuous" in filepath_str:
        return "DiGress continuous"

    elif "DiGress" and "discrete" in filepath_str:
        return "DiGress discrete"

    elif "paccmann" in filepath_str and "vae" in filepath_str:
        return "Paccmann VAE"

    elif "moler" in filepath_str.lower():
        return "MoLeR"

    elif "data_efficient_grammar" in filepath_str.lower():
        return "Data Efficient Grammar"

    elif "rnn" and "selfies" in filepath_str.lower():
        return "RNN Selfies"

    elif "rnn" and "regex" in filepath_str.lower():
        return "RNN Regex"

    elif "rnn" and "char" in filepath_str.lower():
        return "RNN Char"

    else:
        raise RuntimeError(f"Unknown method for {filepath}")


def flat(list_of_lists):
    return [item for sublist in list_of_lists for item in sublist]


if __name__ == "__main__":
    typer.run(main)
