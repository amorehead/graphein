"""Utilities for using structure prediction models.

N.B. ESMFold functionality require torch & ESMFold to be installed.

.. code-block:: bash
    pip install "fair-esm[esmfold]"
    # OpenFold and its remaining dependency
    pip install 'dllogger @ git+https://github.com/NVIDIA/dllogger.git'
    pip install 'openfold @ git+https://github.com/aqlaboratory/openfold.git@4b41059694619831a7db195b7e0988fc4ff3a307'

"""
import os
import subprocess
from functools import lru_cache
from typing import Dict, List, Optional

import requests
from loguru import logger as log

from graphein.utils.dependencies import (
    MissingDependencyError,
    import_message,
    is_tool,
)

try:
    import torch
except ImportError:
    message = import_message(
        "graphein.protein.folding_utils",
        package="torch",
        conda_channel="pytorch",
        pip_install=True,
    )
    log.warning(message)


try:
    import esm
except ImportError:
    message = import_message(
        "graphein.protein.folding_utils",
        package="fair-esm[esmfold]",
        pip_install=True,
    )
    log.warning(message)


@lru_cache
def _get_model(model: str = "v1") -> torch.nn.Module:
    """Loads the ESMFold model."""
    if model == "v1":
        model = esm.pretrained.esmfold_v1()
    elif model == "v0":
        model = esm.pretrained.esmfold_v0()

    model = model.eval().cuda()
    return model


def esmfold(sequence: str, out_path: str):
    """Fold a protein sequence using the ESMFold model.

    Multimer prediction can be done with chains separated by ``:``.

    :param sequence: Amino acid sequence in one-letter code.
    :type sequence: str
    :param out_path: Path to save the PDB file to.
    :type out_path: str
    """

    model = _get_model()

    with torch.no_grad():
        output = model.infer_pdb(sequence)

    with open(out_path, "w") as f:
        f.write(output)


def esmfold_fasta(
    fasta: str,
    out_dir: str,
    num_recycles: int = 4,
    max_tokens: Optional[int] = None,
    chunk_size: Optional[int] = None,
    cpu_only: bool = False,
    cpu_offload: bool = False,
):
    """Batch fold a fasta file with ESMFold.

    Multimer prediction can be done with chains separated by ``:``.

    :param fasta: Path to FASTA file
    :type fasta: str
    :param out_dir: Path to output directory
    :type out_dir: str
    :param num_recycles: Number of recycles to perform. Defaults to number used
        in training (4).
    :type num_recycles: int
    :param max_tokens: Maximum number of tokens per gpu forward-pass. This
        will group shorter sequences together for batched prediction. Lowering
        this can help with out of memory issues, if these occur on short
        sequences.
    :type max_tokens: int
    :param chunk_size: Chunks axial attention computation to reduce memory
        usage from O(L^2) to O(L). Equivalent to running a for loop over chunks
        of of each dimension. Lower values will result in lower memory usage at
        the cost of speed. Recommended values: ``128``, ``64``, ``32``.
        Default: ``None``.
    :type chunk_size: int
    :param cpu_only: CPU only
    :type cpu_only: bool
    :param cpu_offload: Enable CPU offloading
    :type cpu_offload: bool
    :raises FileNotFoundError: If fasta file not found.
    """
    is_tool("esm-fold", error=True)
    if not os.path.exists(fasta):
        raise FileNotFoundError(f"File {fasta} not found.")

    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    command = f"esm-fold --fasta {fasta} "
    command += f"-o {out_dir} "
    command += f"--num-recycles {num_recycles} "
    if max_tokens is not None:
        command += f"--max-tokens-per-batch {max_tokens} "
    if chunk_size is not None:
        command += f"--chunk-size {chunk_size} "
    if cpu_only:
        command += "--cpu-only"
    if cpu_offload:
        command += "--cpu-offload"
    log.info(f"Running command: {command}")
    subprocess.run(command, shell=True)


def esm_embed_fasta(
    fasta: str,
    out_dir: str,
    model: str = "esm2_t33_650M_UR50D",
    repr_layers: Optional[List[int]] = [0, 32, 33],
    include: Optional[List[str]] = ["mean", "per_tok"],
    truncation_seq_length: Optional[int] = None,
):
    """
    Batch embed a fasta file with ESM.


    Default parameters compute final-layer embedding from ESM-2.

    .. code-block:: bash

        python scripts/extract.py esm2_t33_650M_UR50D examples/data/some_proteins.fasta \
        examples/data/some_proteins_emb_esm2 --repr_layers 0 32 33 --include mean per_tok

    :param fasta: Path to FASTA file.
    :type fasta: str
    :param out_dir: Path to output directory.
    :type out_dir: str
    :param model: PyTorch model file OR name of pretrained model to
        download (see README for models) Defaults to ``esm2_t33_650M_UR50D``.
    :param repr_layers: layers indices from which to extract representations
                        (0 to num_layers, inclusive).
    :type repr_layers: List[int]
    :param include: List of representations to include in the output.
        {mean,per_tok,bos,contacts} [{mean,per_tok,bos,contacts} ...]. Default
        is ``["mean", "per_tok"]``.
    :type include: List[str]
    :param truncation_seq_length: Truncate sequences longer than this value.
    :type truncation_seq_length: int
    """
    is_tool("esm-extract", error=True)
    if not os.path.exists(fasta):
        raise FileNotFoundError(f"File {fasta} not found.")

    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    cmd = f"esm-extract {model} {fasta} {out_dir} "
    if repr_layers is not None:
        cmd += f"--repr_layers {' '.join([str(l) for l in repr_layers])} "
    if include is not None:
        cmd += f"--include {' '.join(include)} "
    if truncation_seq_length is not None:
        cmd += f"--truncation_seq_length {truncation_seq_length} "
    log.info(f"Running command: {cmd}")
    subprocess.run(cmd, shell=True)


def esmfold_web(
    sequence: str, out_path: Optional[str] = None, version: int = 1
):
    """Fold a protein sequence using the ESMFold model from the ESMFold server
    at https://api.esmatlas.com/foldSequence/v1/pdb/.

    Parameters
    ----------
    sequence : str
        A protein sequence in one-letter code.
    out_path : str, optional
        Path to save the PDB file to. If `None`, the file is not saved.
        Defaults to `None`.
    version : int, optional
        The version of the ESMFold model to use. Defaults to `1`.
    """
    URL = f"https://api.esmatlas.com/foldSequence/v{version}/pdb/"

    headers: Dict[str, str] = {
        "Content-Type": "application/x-www-form-urlencoded",
    }

    cif = requests.post(URL, data=sequence, headers=headers).text
    # append header
    header = "\n".join(
        [f"data_{sequence}", "#", f"_entry.id\t{sequence}", "#\n"]
    )
    cif = header + cif
    if out_path is not None:
        with open(out_path, "w") as f:
            f.write(cif)
