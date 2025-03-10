"""
Generate the vocabulary of the selfies of the smiles in the dataset
"""
import typer
import yaml
import selfies as sf
from tqdm import tqdm


def read_smiles_file(path, percentage):
    with open(path, "r") as f:
        smiles = [line.strip("\n") for line in f.readlines()]
    num_data = len(smiles)
    return smiles[0 : int(num_data * percentage)]


def main(dataset_path, output_vocab):
    smiles = read_smiles_file(dataset_path, 1)
    selfies = []
    for x in tqdm(smiles):
        x = sf.encoder(x)
        if x is not None:
            selfies.append(x)

    print("getting alphabet from selfies...")
    vocab = sf.get_alphabet_from_selfies(selfies)

    vocab_dict = {}
    for i, token in enumerate(vocab):
        vocab_dict[token] = i

    i += 1
    vocab_dict["<eos>"] = i
    i += 1
    vocab_dict["<sos>"] = i
    i += 1
    vocab_dict["<pad>"] = i

    with open(output_vocab, "w") as f:
        yaml.dump(vocab_dict, f)


if __name__ == "__main__":
    typer.run(main)
