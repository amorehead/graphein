import gzip
import os
import shutil
import subprocess
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Union

import numpy as np
import pandas as pd
import wget
from loguru import logger as log
from tqdm import tqdm

from graphein.protein.utils import (
    download_pdb_multiprocessing,
    extract_chains_to_file,
    is_tool,
    read_fasta,
)


class PDBManager:
    """A utility for creating selections of experimental PDB structures."""

    def __init__(
        self,
        root_dir: str = ".",
        splits: Optional[List[str]] = None,
        split_ratios: Optional[List[float]] = None,
        split_time_frames: Optional[List[np.datetime64]] = None,
        assign_leftover_rows_to_split_n: int = 0,
    ):
        """Instantiate a selection of experimental PDB structures.

        :param root_dir: The directory in which to store all PDB entries,
            defaults to ``"."``.
        :type root_dir: str, optional
        :param splits: A list of names corresponding to each dataset split,
            defaults to ``None``.
        :type splits: Optional[List[str]], optional
        :param split_ratios: Proportions into which to split the current
            selection of PDB entries, defaults to ``None``.
        :type split_ratios: Optional[List[float]], optional
        :param split_time_frames: Time periods into which to segment the current
            selection of PDB entries, defaults to ``None``.
        :type split_time_frames: Optional[List[np.datetime64]], optional
        :param assign_leftover_rows_to_split_n: Index of the split to which
            to assign any rows remaining after creation of new dataset splits,
            defaults to ``0``.
        :type assign_leftover_rows_to_split_n: int, optional
        """
        # Arguments
        self.root_dir = Path(root_dir)

        # Constants
        self.pdb_sequences_url = (
            "https://ftp.wwpdb.org/pub/pdb/derived_data/pdb_seqres.txt.gz"
        )
        self.ligand_map_url = (
            "http://ligand-expo.rcsb.org/dictionaries/cc-to-pdb.tdd"
        )
        self.source_map_url = (
            "https://files.wwpdb.org/pub/pdb/derived_data/index/source.idx"
        )
        self.resolution_url = (
            "https://files.wwpdb.org/pub/pdb/derived_data/index/resolu.idx"
        )
        self.pdb_entry_type_url = (
            "https://files.wwpdb.org/pub/pdb/derived_data/pdb_entry_type.txt"
        )
        self.pdb_deposition_date_url = (
            "https://files.wwpdb.org/pub/pdb/derived_data/index/entries.idx"
        )

        self.pdb_dir = self.root_dir / "pdb"
        if not os.path.exists(self.pdb_dir):
            os.makedirs(self.pdb_dir)

        self.pdb_seqres_archive_filename = Path(self.pdb_sequences_url).name
        self.pdb_seqres_filename = Path(self.pdb_seqres_archive_filename).stem
        self.ligand_map_filename = Path(self.ligand_map_url).name
        self.source_map_filename = Path(self.source_map_url).name
        self.resolution_filename = Path(self.resolution_url).name
        self.pdb_entry_type_filename = Path(self.pdb_entry_type_url).name
        self.pdb_deposition_date_filename = Path(
            self.pdb_deposition_date_url
        ).name

        self.list_columns = ["ligands"]

        # Data
        self.download_metadata()
        self.df = self.parse()
        self.source = self.df.copy()

        # Splits
        self.splits_provided = splits is not None
        if self.splits_provided:
            assert len(set(splits)) == len(splits)
            self.splits = splits
            self.df_splits = {split: None for split in splits}
            self.assign_leftover_rows_to_split_n = (
                assign_leftover_rows_to_split_n
            )
            # Sequence-based ratio splits
            if split_ratios is not None:
                assert len(splits) == len(split_ratios)
                assert sum(split_ratios) == 1.0
                self.split_ratios = split_ratios
            # Time-based splits
            if split_time_frames is not None:
                assert len(splits) == len(split_time_frames)
                assert self._frames_are_sequential(split_time_frames)
                self.split_time_frames = split_time_frames

    def download_metadata(self):
        """Download all PDB metadata."""
        self._download_pdb_sequences()
        self._download_ligand_map()
        self._download_source_map()
        self._download_resolution()
        self._download_entry_metadata()

    @property
    def num_unique_pdbs(self) -> int:
        """Return the number of unique PDB IDs in the dataset.

        :return: Number of unique PDB IDs.
        :rtype: int
        """
        return len(self.df.pdb.unique())

    @property
    def unique_pdbs(self) -> List[str]:
        """Return a list of unique PDB IDs in the dataset.

        :return: List of unique PDB IDs.
        :rtype: List[str]
        """
        return self.df.pdb.unique().tolist()

    @property
    def num_chains(self) -> int:
        """Return the number of chains in the dataset.

        :return: Number of chains.
        :rtype: int
        """
        return len(self.df)

    @property
    def longest_chain(self) -> int:
        """Return the length of the longest chain in the dataset.

        :return: Length of the longest chain.
        :rtype: int
        """
        return self.df.length.max()

    @property
    def shortest_chain(self) -> int:
        """Return the length of the shortest chain in the dataset.

        :return: Length of the shortest chain.
        :rtype: int
        """
        return self.df.length.min()

    @property
    def best_resolution(self) -> float:
        """Return the best resolution in the dataset.

        :return: Best resolution.
        :rtype: float
        """
        return self.df.resolution.min()

    @property
    def worst_resolution(self) -> float:
        """Return the worst resolution in the dataset.

        :return: Worst resolution.
        :rtype: float
        """
        return self.df.resolution.max()

    def _frames_are_sequential(
        self, split_time_frames: List[np.datetime64]
    ) -> bool:
        """Check if all provided frames are sequentially ordered.

        :param split_time_frames: Time frames into which to split
            selected PDB entries.
        :type split_time_frames: List[np.datetime64]

        :return: Whether all provided frames are sequentially ordered.
        :rtype: bool
        """
        frames_are_sequential = True
        last_frame_index = len(split_time_frames) - 1
        for frame_index in range(len(split_time_frames)):
            frame = split_time_frames[frame_index]
            frames_are_backwards_sequential = frame_index == 0 or (
                frame_index > 0 and frame > split_time_frames[frame_index - 1]
            )
            frames_are_forwards_sequential = (
                frame_index < last_frame_index
                and frame < split_time_frames[frame_index + 1]
            ) or frame_index == last_frame_index
            frames_are_sequential = all(
                [
                    frames_are_backwards_sequential,
                    frames_are_forwards_sequential,
                ]
            )
        return frames_are_sequential

    def _download_pdb_sequences(self):
        # Download https://ftp.wwpdb.org/pub/pdb/derived_data/pdb_seqres.txt.gz
        if not os.path.exists(
            self.root_dir / self.pdb_seqres_archive_filename
        ):
            log.info("Downloading PDB sequences...")
            wget.download(self.pdb_sequences_url)
            log.info("Downloaded sequences")

        # Unzip all collected sequences
        if not os.path.exists(self.root_dir / self.pdb_seqres_filename):
            log.info("Unzipping PDB sequences...")
            with gzip.open(
                self.root_dir / self.pdb_seqres_archive_filename, "rb"
            ) as f_in:
                with open(
                    self.root_dir / self.pdb_seqres_filename, "wb"
                ) as f_out:
                    shutil.copyfileobj(f_in, f_out)
            log.info("Unzipped sequences")

    def _download_ligand_map(self):
        # http://ligand-expo.rcsb.org/dictionaries/cc-to-pdb.tdd
        if not os.path.exists(self.root_dir / self.ligand_map_filename):
            log.info("Downloading ligand map...")
            wget.download(self.ligand_map_url)
            log.info("Downloaded ligand map")

    def _download_source_map(self):
        # https://files.wwpdb.org/pub/pdb/derived_data/index/source.idx
        if not os.path.exists(self.root_dir / self.source_map_filename):
            log.info("Downloading source map...")
            wget.download()
            log.info("Downloaded source map")

    def _download_resolution(self):
        # https://files.wwpdb.org/pub/pdb/derived_data/index/resolu.idx
        if not os.path.exists(self.root_dir / self.resolution_filename):
            log.info("Downloading resolution map...")
            wget.download(self.resolution_url)
            log.info("Downloaded resolution map")

    def _download_entry_metadata(self):
        if not os.path.exists(self.root_dir / "entries.idx"):
            log.info("Downloading entry metadata...")
            wget.download(self.pdb_deposition_date_url)
            log.info("Downloaded entry metadata")

    def _download_exp_type(self):
        # https://files.wwpdb.org/pub/pdb/derived_data/pdb_entry_type.txt
        if not os.path.exists(self.root_dir / self.pdb_entry_type_filename):
            log.info("Downloading experiment type map...")
            wget.download(self.pdb_entry_type_url)
            log.info("Downloaded experiment type map")

    def _parse_ligand_map(self) -> Dict[str, List[str]]:
        """Parse the ligand maps for all PDB records.

        :return: Dictionary of PDB entries with their
            corresponding ligand map values.
        :rtype: Dict[str, List[str]]
        """
        ligand_map = {}
        with open(self.root_dir / self.ligand_map_filename) as f:
            for line in f:
                line = line.strip()
                params = line.split()
                ligand_map[params[0]] = params[1:]
        inv = {}
        for k, v in ligand_map.items():
            for x in v:
                inv.setdefault(x, []).append(str(k))
        return inv

    def _parse_source_map(self) -> Dict[str, str]:
        """Parse the source maps for all PDB records.

        :return: Dictionary of PDB entries with their
            corresponding source map values.
        :rtype: Dict[str, str]
        """
        source_map = {}
        with open(self.source_map_filename) as f:
            for line in f:
                line = line.strip()
                params = line.split()
                if params[0] in {
                    "Mon",
                    "Tue",
                    "Wed",
                    "Thu",
                    "Fri",
                    "Sat",
                    "Sun",
                }:
                    continue
                source_map[params[0].lower()] = " ".join(params[1:])

        del source_map["protein"]
        del source_map["idcode"]
        del source_map["------"]
        return source_map

    def _parse_resolution(self) -> Dict[str, float]:
        """Parse the PDB resolutions for all PDB records.

        :return: Dictionary of PDB resolutions with their
            corresponding values.
        :rtype: Dict[str, float]
        """
        res = {}
        with open(self.root_dir / self.resolution_filename) as f:
            for line in f:
                line = line.strip()
                params = line.split()
                if not params or len(params) != 3:
                    continue
                pdb = params[0]
                resolution = params[2]
                try:
                    res[pdb.lower()] = float(resolution)
                except ValueError:
                    continue
        return res

    def _parse_entries(self) -> Dict[str, datetime]:
        with open(self.root_dir / self.pdb_deposition_date_filename, "r") as f:
            lines = f.readlines()
        lines = lines[2:]  # Skip header
        # Note: There's a badly formatted line we need to deal with instead of
        # using Pandas' builtin CSV parser.
        lines = [l.replace('"', "") for l in lines]

        df = pd.read_csv(
            StringIO("".join(lines)),
            sep="\t",
            header=None,
            skipinitialspace=True,
        )
        df.columns = [
            "id",
            "name",
            "date",
            "title",
            "source",
            "authors",
            "resolution",
            "experiment_type",
        ]
        df.dropna(subset=["id"], inplace=True)

        df.id = df.id.str.lower()
        df.date = pd.to_datetime(df.date)
        return pd.Series(df["date"].values, index=df["id"]).to_dict()

    def _parse_experiment_type(self) -> Dict[str, str]:
        """Parse the experiment types for all PDB records.

        :return: Dictionary of PDB entries with their
            corresponding experiment types.
        :rtype: Dict[str, str]
        """
        df = pd.read_csv(
            self.root_dir / self.pdb_entry_type_filename, sep="\t", header=None
        )
        df.dropna(inplace=True)
        return pd.Series(df[2].values, index=df[0]).to_dict()

    def parse(self) -> pd.DataFrame:
        """Parse all PDB sequence records.

        :return: DataFrame containing PDB sequence entries
            with their corresponding metadata.
        :rtype: pd.DataFrame
        """
        fasta = read_fasta(self.pdb_seqres_filename)

        # Iterate over fasta and parse metadata
        records = []
        for k, v in fasta.items():
            seq = v
            params = k.split()
            pdb_id = params[0]
            pdb = params[0].split("_")[0]
            chain = params[0].split("_")[1]
            length = int(params[2].split(":")[1])
            molecule_type = params[1].split(":")[1]
            name = " ".join(params[3:])
            split = "N/A"  # Assign rows to the null split
            record = {
                "id": pdb_id,
                "pdb": pdb,
                "chain": chain,
                "length": length,
                "molecule_type": molecule_type,
                "name": name,
                "sequence": seq,
                "split": split,
            }
            records.append(record)

        df = pd.DataFrame.from_records(records)
        df["ligands"] = df.pdb.map(self._parse_ligand_map())
        df["ligands"] = df["ligands"].fillna("").apply(list)
        df["source"] = df.pdb.map(self._parse_source_map())
        df["resolution"] = df.pdb.map(self._parse_resolution())
        df["deposition_date"] = df.pdb.map(self._parse_entries())
        df["experiment_type"] = df.pdb.map(self._parse_experiment_type())

        return df

    def sample(
        self,
        n: Optional[int] = None,
        frac: Optional[float] = None,
        replace: bool = False,
        update: bool = False,
    ) -> pd.DataFrame:
        """Sample a subset of the dataset.

        :param n: Number of molecules to select, defaults to ``None``.
        :type n: Optional[int], optional
        :param frac: Fraction of molecules to select, defaults to ``None``.
        :type frac: Optional[float], optional
        :param replace: Whether or not to sample with replacement, defaults to
            ``False``.
        :type replace: bool, optional
        :param update: Whether or not to update the DataFrame in place,
            defaults to ``False``.
        :type update: bool, optional

        :return: DataFrame of sampled molecules.
        :rtype: pd.DataFrame
        """
        df = self.df.sample(n=n, frac=frac, replace=replace)
        if update:
            self.df = df
        return df

    def molecule_type(
        self, type: str = "protein", update: bool = False
    ) -> pd.DataFrame:
        """Select molecules by molecule type. [`protein`, `dna`, `rna`]

        :param type: Type of molecule, defaults to "protein".
        :type type: str, optional
        :param update: Whether to modify the DataFrame in place, defaults to
            ``False``.
        :type update: bool, optional

        :return: DataFrame of selected molecules.
        :rtype: pd.DataFrame
        """
        df = self.df.loc[self.df.molecule_type == type]

        if update:
            self.df = df
        return df

    def experiment_type(
        self, type: str = "diffraction", update: bool = False
    ) -> pd.DataFrame:
        """Select molecules by experiment type. [`diffraction`, `NMR`, `EM`, `other`]

        :param type: Experiment type of molecule, defaults to "diffraction".
        :type type: str, optional
        :param update: Whether to modify the DataFrame in place, defaults to
            ``False``.
        :type update: bool, optional

        :return: DataFrame of selected molecules.
        :rtype: pd.DataFrame
        """
        df = self.df.loc[self.df.experiment_type == type]

        if update:
            self.df = df
        return df

    def longer_than(self, length: int, update: bool = False) -> pd.DataFrame:
        """Select molecules longer than a given length.

        :param length: Minimum length of molecule.
        :type length: int
        :param update: Whether to modify the DataFrame in place, defaults to
            ``False``.
        :type update: bool, optional

        :return: DataFrame of selected molecules.
        :rtype: pd.DataFrame
        """
        df = self.df.loc[self.df.length > length]

        if update:
            self.df = df
        return df

    def shorter_than(self, length: int, update: bool = False) -> pd.DataFrame:
        """
        Select molecules shorter than a given length.

        :param length: Maximum length of molecule.
        :type length: int
        :param update: Whether to modify the DataFrame in place, defaults to
            ``False``.
        :type update: bool, optional

        :return: DataFrame of selected molecules.
        :rtype: pd.DataFrame
        """
        df = self.df.loc[self.df.length < length]

        if update:
            self.df = df
        return df

    def resolution_better_than_or_equal_to(
        self, resolution: int, update: bool = False
    ) -> pd.DataFrame:
        """Select molecules with a resolution better than or equal to the given value.

        Conventions for PDB resolution values are used, where a lower resolution
        value indicates a better resolution for a molecule overall.

        :param resolution: Worst molecule resolution allowed.
        :type resolution: int
        :param update: Whether to modify the DataFrame in place, defaults to
            ``False``.
        :type update: bool, optional

        :return: DataFrame of selected molecules.
        :rtype: pd.DataFrame
        """
        df = self.df.loc[self.df.resolution <= resolution]

        if update:
            self.df = df
        return df

    def resolution_worse_than_or_equal_to(
        self, resolution: int, update: bool = False
    ) -> pd.DataFrame:
        """Select molecules with a resolution worse than or equal to the given value.

        Conventions for PDB resolution values are used, where a higher resolution
        value indicates a worse resolution for a molecule overall.

        :param resolution: Best molecule resolution allowed.
        :type resolution: int
        :param update: Whether to modify the DataFrame in place, defaults to
            ``False``.
        :type update: bool, optional

        :return: DataFrame of selected molecules.
        :rtype: pd.DataFrame
        """
        df = self.df.loc[self.df.resolution >= resolution]

        if update:
            self.df = df
        return df

    def oligomeric(
        self,
        oligomer: int = 1,
        comparison: str = "equal",
        update: bool = False,
    ):
        """Select molecules with a given oligmeric length.

        :param length: Oligomeric length of molecule, defaults to ``1``.
        :type length: int
        :param comparison: Comparison operator. One of ``"equal"``,
            ``"less"``, or ``"greater"``, defaults to ``"equal"``.
        :param update: Whether to modify the DataFrame in place, defaults to
            ``False``.
        :type update: bool, optional

        :return: DataFrame of selected oligmers.
        :rtype: pd.DataFrame
        """
        if comparison == "equal":
            df = self.df[
                self.df.groupby("pdb")["pdb"].transform("size") == oligomer
            ]
        elif comparison == "less":
            df = self.df[
                self.df.groupby("pdb")["pdb"].transform("size") < oligomer
            ]
        elif comparison == "greater":
            df = self.df[
                self.df.groupby("pdb")["pdb"].transform("size") > oligomer
            ]
        else:
            raise ValueError(
                "Comparison must be one of 'equal', 'less', or 'greater'."
            )

        if update:
            self.df = df
        return df

    def has_ligand(self, ligand: str, update: bool = False) -> pd.DataFrame:
        """
        Select molecules that contain a given ligand.

        :param ligand: Ligand to select. (PDB ligand code - http://ligand-expo.rcsb.org/)
        :type ligand: str
        :param update: Whether to update the DataFrame in place, defaults to
            ``False``.
        :type update: bool, optional

        :return: DataFrame of selected molecules.
        :rtype: pd.DataFrame
        """
        df = self.df.loc[self.df.ligands.map(lambda x: ligand in x)]

        if update:
            self.df = df
        return df

    def has_ligands(
        self, ligands: List[str], inverse: bool = False, update: bool = False
    ):
        """Select molecules that contain all ligands in the provided list.

        If inverse is ``True``, selects molecules that do not have all the
        ligands in the list.

        :param ligand: List of ligands. (PDB ligand codes - http://ligand-expo.rcsb.org/)
        :type ligand: List[str]
        :param inverse: Whether to inverse the selection, defaults to ``False``.
        :type inverse: bool, optional
        :param update: Whether to update the DataFrame in place, defaults to
            ``False``.
        :type update: bool, optional

        :return: DataFrame of selected molecules.
        :rtype: pd.DataFrame
        """
        if inverse:
            df = self.df.loc[
                self.df.ligands.map(lambda x: not set(ligands).issubset(x))
            ]
        else:
            df = self.df.loc[
                self.df.ligands.map(lambda x: set(ligands).issubset(x))
            ]

        if update:
            self.df = df
        return df

    def to_chain_sequence_mapping_dict(self) -> Dict[str, str]:
        """Return a dictionary of sequences indexed by chains.

        :return: Dictionary of chain-sequence mappings.
        :rtype: Dict[str, str]
        """
        return (
            self.df[["id", "sequence"]].set_index("id").to_dict()["sequence"]
        )

    def to_fasta(self, filename: str):
        """Write the dataset to a FASTA file (indexed by chain id).

        :param filename: Name of the output FASTA file.
        :type filename: str
        """
        with open(filename, "w") as f:
            for k, v in self.to_chain_sequence_mapping_dict().items():
                f.write(f">{k}\n")
                f.write(f"{v}\n")

    def remove_non_standard_alphabet_sequences(self, update: bool = False):
        """
        Remove sequences with non-standard characters.

        :param update: Whether to update the DataFrame in place, defaults to
            ``False``.
        :type update: bool, optional

        :return: DataFrame containing only sequences with standard characters.
        :rtype: pd.DataFrame
        """
        df = self.df.loc[
            self.df.sequence.map(
                lambda x: set(x).issubset(set("ACDEFGHIKLMNPQRSTVWY"))
            )
        ]
        if update:
            self.df = df
        return df

    def download(
        self,
        out_dir=".",
        overwrite: bool = False,
        max_workers: int = 8,
        chunksize: int = 32,
    ):
        """Download PDB files in the current selection.

        :param out_dir: Output directory, defaults to ``"."``
        :type out_dir: str, optional
        :param overwrite: Overwrite existing files, defaults to ``False``.
        :type overwrite: bool, optional
        :param max_workers: Number of processes to use, defaults to ``8``.
        :type max_workers: int, optional
        :param chunksize: Chunk size for each worker, defaults to ``32``.
        :type chunksize: int, optional
        """
        log.info(f"Downloading {len(self.unique_pdbs)} PDB files...")
        download_pdb_multiprocessing(
            self.unique_pdbs,
            out_dir,
            overwrite=overwrite,
            max_workers=max_workers,
            chunksize=chunksize,
        )

    def write_chains(self) -> List[Path]:
        """Write chains in current selection to disk. e.g., we create a file
        of the form ``4hbb_A.pdb`` for chain ``A`` of PDB file ``4hhb.pdb``.

        If the PDB files are not contained in ``self.pdb_dir``, they are
        downloaded.

        :return: List of paths to written files.
        :rtype: List[Path]
        """
        # Get dictionary of PDB code : List[Chains]
        df = self.df.groupby("pdb")["chain"].agg(list).to_dict()

        # Check we have all source PDB files
        downloaded = os.listdir(self.pdb_dir)
        downloaded = [f for f in downloaded if f.endswith(".pdb")]

        to_download = [k for k in df.keys() if f"{k}.pdb" not in downloaded]
        if len(to_download) > 0:
            log.info(f"Downloading {len(to_download)} PDB files...")
            download_pdb_multiprocessing(
                to_download, self.pdb_dir, overwrite=True
            )
            log.info("Done downloading PDB files")

        # Iterate over dictionary and write chains to separate files
        log.info("Extracting chains...")
        paths = []
        for k, v in tqdm(df.items()):
            in_file = os.path.join(self.pdb_dir, f"{k}.pdb")
            paths.append(
                extract_chains_to_file(in_file, v, out_dir=self.pdb_dir)
            )
        log.info("Done extracting chains")

        # Flatten list of paths
        return [Path(num) for sublist in paths for num in sublist]

    def reset(self) -> pd.DataFrame:
        """Reset the dataset to the original DataFrame source.

        :return: The source dataset DataFrame.
        :rtype: pd.DataFrame
        """
        self.df = self.source.copy()
        return self.df

    def split_df_proportionally(
        self,
        df: pd.DataFrame,
        splits: List[str],
        split_ratios: List[float],
        assign_leftover_rows_to_split_n: int = 0,
        random_state: int = 42,
    ) -> Dict[str, pd.DataFrame]:
        """Split the provided DataFrame iteratively according to given proportions.

        :param df: DataFrame to split.
        :type df: pd.DataFrame
        :param splits: Names of splits into which to divide the provided DataFrame.
        :type splits: List[str]
        :param split_ratios: Ratios by which to split the provided DataFrame.
        :type split_ratios: List[float]
        :param assign_leftover_rows_to_split_n: To which split to assign leftover rows,
            defaults to ``0``.
        :type assign_leftover_rows_to_split_n: int, optional
        :param random_state: Random seed to use for DataFrame splitting, defaults to
            ``42``.
        :type random_state: int, optional

        :return: Dictionary of DataFrame splits.
        :rtype: Dict[str, pd.DataFrame]
        """
        assert len(splits) == len(split_ratios)
        assert sum(split_ratios) == 1

        # Calculate the size of each split
        split_sizes = [int(len(df) * ratio) for ratio in split_ratios]

        # Assign leftover rows to a specified split
        num_remaining_rows = len(df) - sum(split_sizes)
        if num_remaining_rows > 0:
            split_sizes[assign_leftover_rows_to_split_n] += num_remaining_rows

        # Without replacement, randomly shuffle rows within the input DataFrame
        df_sampled = df.sample(
            frac=1.0, replace=False, random_state=random_state
        )

        # Split DataFrames
        start_idx = 0
        df_splits = {}
        for split_index, split_size in enumerate(split_sizes):
            split = splits[split_index]
            end_idx = start_idx + split_size
            df_split = df_sampled.iloc[start_idx:end_idx]
            df_splits[split] = df_split
            start_idx = end_idx

        # Ensure there are no duplicated rows between splits
        all_rows = pd.concat([df_splits[split] for split in splits])
        assert len(all_rows) == len(
            df
        ), "Number of rows changed during split operations."
        assert len(
            all_rows.drop(self.list_columns, axis=1).drop_duplicates()
        ) == len(df), "Duplicate rows found in splits."

        df_split_sizes = " ".join(
            [str(df_splits[split].shape[0]) for split in df_splits]
        )
        log.info(
            f"Proportionally-derived dataset splits of sizes: {df_split_sizes}"
        )

        return df_splits

    def merge_df_splits(
        self,
        first_df_split: pd.DataFrame,
        second_df_split: pd.DataFrame,
        split: str,
    ) -> pd.DataFrame:
        """Reconcile an existing DataFrame split with a new split.

        :param first_df_split: Existing DataFrame split.
        :type first_df_split: pd.DataFrame
        :param second_df_split: New DataFrame split.
        :type second_df_split: pd.DataFrame
        :param split: Name of DataFrame split.
        :type split: str

        :return: Merged DataFrame split.
        :rtype: pd.DataFrame
        """
        # Coerce list columns into tuple columns
        # Ref: https://stackoverflow.com/questions/45991496/merging-dataframes-with-unhashable-columns
        for df_split in [first_df_split, second_df_split]:
            for list_column in self.list_columns:
                if list_column in df_split.columns:
                    df_split[list_column] = df_split[list_column].apply(tuple)
        # Merge DataFrame splits
        merge_columns = first_df_split.columns.to_list()
        merged_df_split = pd.merge(
            first_df_split, second_df_split, how="inner", on=merge_columns
        )
        # Coerce tuple columns back into list columns
        for df_split in [first_df_split, second_df_split]:
            for list_column in self.list_columns:
                if list_column in df_split.columns:
                    df_split[list_column] = df_split[list_column].apply(list)
        # Track split names
        merged_df_split["split"] = split
        return merged_df_split

    def split_clusters(
        self,
        df: pd.DataFrame,
        update: bool = False,
    ) -> Dict[str, pd.DataFrame]:
        """Split clusters derived by MMseqs2.

        :param df: DataFrame containing the clusters derived by MMseqs2.
        :type df: pd.DataFrame
        :param update: Whether to update the selection to the representative
            sequences, defaults to ``False``.
        :type update: bool, optional

        :return: A Dictionary of split names mapping to DataFrames of
            randomly-split representative sequences.
        :rtype: Dict[str, pd.DataFrame]
        """
        split_ratios_provided = self.split_ratios is not None
        assert split_ratios_provided

        # Split clusters
        log.info(
            f"Randomly splitting clusters into ratios: {' '.join([str(r) for r in self.split_ratios])}..."
        )
        df_splits = self.split_df_proportionally(
            df,
            self.splits,
            self.split_ratios,
            self.assign_leftover_rows_to_split_n,
        )
        log.info("Done splitting clusters")

        # Update splits
        for split in self.splits:
            if update:
                df_split = df_splits[split]
                if self.df_splits[split] is not None:
                    self.df_splits[split] = self.merge_df_splits(
                        self.df_splits[split], df_split, split
                    )
                else:
                    self.df_splits[split] = df_split
                df_splits[split] = self.df_splits[split]

        return df_splits

    def cluster(
        self,
        min_seq_id: float = 0.3,
        coverage: float = 0.8,
        update: bool = False,
    ) -> Union[pd.DataFrame, Dict[str, pd.DataFrame]]:
        """Cluster sequences in selection using MMseqs2.

        :param min_seq_id: Sequence identity, defaults to ``0.3``.
        :type min_seq_id: float, optional
        :param coverage: Clustering coverage, defaults to ``0.8``.
        :type coverage: float, optional
        :param update: Whether to update the selection to the representative
            sequences, defaults to ``False``.
        :type update: bool, optional
        :return: Either a single DataFrame of representative sequences or a
            Dictionary of split names mapping to DataFrames of randomly-split
            representative sequences.
        :rtype: Union[pd.DataFrame, Dict[str, pd.DataFrame]]
        """
        # Write fasta
        self.to_fasta("pdb.fasta")
        if not is_tool("mmseqs"):
            log.error(
                "MMseqs2 not found. Please install it: conda install -c conda-forge -c bioconda mmseqs2"
            )

        # Create clusters
        if not os.path.exists("pdb_cluster_rep_seq.fasta"):
            cmd = f"mmseqs easy-cluster pdb.fasta pdb_cluster tmp --min-seq-id {min_seq_id} -c {coverage} --cov-mode 1"
            log.info(f"Clustering with: {cmd}")
            subprocess.run(cmd.split())
            log.info("Done with clustering")

        # Read fasta
        df = self.from_fasta(ids="chain", filename="pdb_cluster_rep_seq.fasta")
        if update:
            self.df = df

        # Split fasta
        if self.splits_provided:
            return self.split_clusters(df, update)

        return df

    def split_df_into_time_frames(
        self,
        df: pd.DataFrame,
        splits: List[str],
        split_time_frames: List[np.datetime64],
    ) -> Dict[str, pd.DataFrame]:
        """Split the provided DataFrame sequentially according to given time frames.

        :param df: DataFrame to split.
        :type df: pd.DataFrame
        :param splits: Names of splits into which to divide the provided DataFrame.
        :type splits: List[str]
        :param split_time_frames: Time frames into which to split the provided DataFrame.
        :type split_time_frames: List[np.datetime64]

        :return: Dictionary of DataFrame splits.
        :rtype: Dict[str, pd.DataFrame]
        """
        assert len(splits) == len(split_time_frames)
        assert self._frames_are_sequential(split_time_frames)

        # Split DataFrames
        start_datetime = df.deposition_date.min()
        df_splits = {}
        for split_index in range(len(splits)):
            split = splits[split_index]
            end_datetime = split_time_frames[split_index]
            df_split = df.loc[
                (df.deposition_date >= start_datetime)
                & (df.deposition_date < end_datetime)
            ]
            df_splits[split] = df_split
            start_datetime = end_datetime

        # Identify any remaining rows
        start_datetime = end_datetime
        end_datetime = df.deposition_date.max()
        num_remaining_rows = df.loc[
            (df.deposition_date >= start_datetime)
            & (df.deposition_date <= end_datetime)
        ].shape[0]

        # Ensure there are no duplicated rows between splits
        all_rows = pd.concat([df_splits[split] for split in splits])
        assert (
            len(all_rows) == len(df) - num_remaining_rows
        ), "Number of rows changed during split operations."
        assert (
            len(all_rows.drop(self.list_columns, axis=1).drop_duplicates())
            == len(df) - num_remaining_rows
        ), "Duplicate rows found in splits."

        df_split_sizes = " ".join(
            [str(df_splits[split].shape[0]) for split in df_splits]
        )
        log.info(
            f"Deposition date-derived dataset splits of sizes: {df_split_sizes}"
        )

        return df_splits

    def split_by_deposition_date(
        self,
        df: pd.DataFrame,
        update: bool = False,
    ) -> Dict[str, pd.DataFrame]:
        """Split molecules based on their deposition date.

        :param df: DataFrame containing the molecule sequences to split.
        :type df: pd.DataFrame
        :param update: Whether to update the selection to the PDB entries
            defaults to ``False``.
        :type update: bool, optional

        :return: A Dictionary of split names mapping to DataFrames of
            sequence splits based on the sequential time frames given.
        :rtype: Dict[str, pd.DataFrame]
        """
        split_time_frames_provided = self.split_time_frames is not None
        assert split_time_frames_provided

        # Split sequences
        time_frames = " ".join([str(f) for f in self.split_time_frames])
        log.info(f"Splitting sequences into time frames: {time_frames}")
        df_splits = self.split_df_into_time_frames(
            df, self.splits, self.split_time_frames
        )
        log.info("Done splitting sequences")

        # Update splits
        for split in self.splits:
            if update:
                df_split = df_splits[split]
                if self.df_splits[split] is not None:
                    self.df_splits[split] = self.merge_df_splits(
                        self.df_splits[split], df_split, split
                    )
                else:
                    self.df_splits[split] = df_split
                df_splits[split] = self.df_splits[split]

        return df_splits

    def filter_by_deposition_date(
        self, max_deposition_date: np.datetime64, update: bool = False
    ) -> Union[pd.DataFrame, Dict[str, pd.DataFrame]]:
        """
        Select molecules deposited on or before a given date.

        :param max_deposition_date: Maximum deposition date of molecule.
        :type max_deposition_date: np.datetime64
        :param update: Whether to modify the DataFrame in place, defaults to
            ``False``.
        :type update: bool, optional

        :return: Either a single DataFrame of sequences or a
            Dictionary of split names mapping to DataFrames of
            sequences split successively by their deposition date.
        :rtype: Union[pd.DataFrame, Dict[str, pd.DataFrame]]
        """
        # Drop missing deposition dates
        df = self.df.dropna().loc[
            self.df.deposition_date < max_deposition_date
        ]
        if update:
            self.df = df

        # Split sequences
        if self.splits_provided:
            return self.split_by_deposition_date(df, update)

        return df

    def from_fasta(self, ids: str, filename: str) -> pd.DataFrame:
        """Create a selection from a FASTA file.

        :param ids: Whether the FASTA is indexed by chains (i.e., ``3eiy_A``)
            or PDB ids (``3eiy``).
        :type ids: str
        :param filename: Name of FASTA file.
        :type filename: str

        :return: DataFrame of selected molecules.
        :rtype: pd.DataFrame
        """
        fasta = read_fasta(filename)
        seq_ids = list(fasta.keys())
        if ids == "chain":
            return self.source.loc[self.source.id.isin(seq_ids)]
        elif ids == "pdb":
            return self.source.loc[self.source.pdb.isin(seq_ids)]


if __name__ == "__main__":
    pdb_manager = PDBManager(
        root_dir=".",
        splits=["train", "val", "test"],
        split_ratios=[0.8, 0.1, 0.1],
        split_time_frames=[
            np.datetime64("2022-01-01"),
            np.datetime64("2022-05-01"),
            np.datetime64("2023-01-01"),
        ],
    )

    pdb_manager.molecule_type(type="protein", update=True)
    pdb_manager.experiment_type(type="diffraction", update=True)
    pdb_manager.resolution_better_than_or_equal_to(2.0, update=True)

    print(f"cluster_dfs: {pdb_manager.cluster(update=True)}")
    print(
        f"time_frame_split_dfs: \
            {pdb_manager.filter_by_deposition_date(max_deposition_date=np.datetime64('2023-02-01'), update=True)}"
    )
