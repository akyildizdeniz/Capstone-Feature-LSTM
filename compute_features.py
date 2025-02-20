"""This module is used for computing social and map features for motion forecasting baselines.

Example usage:
    $ python compute_features.py --data_dir ~/val/data 
        --feature_dir ~/val/features --mode val
"""

import os
import shutil
import tempfile
import time
from typing import Any, Dict, List, Tuple

import argparse
from joblib import Parallel, delayed
import numpy as np
import pandas as pd
import pickle as pkl
import sys

from argoverse.map_representation.map_api import ArgoverseMap

from utils.baseline_config import RAW_DATA_FORMAT, _FEATURES_SMALL_SIZE, FEATURE_TYPES
from utils.map_features_utils import MapFeaturesUtils
from utils.social_features_utils import SocialFeaturesUtils
from utils.compute_features_utils import compute_physics_features 
from utils.compute_features_utils import save_ml_physics_features
from utils.compute_semantic_features import compute_semantic_features 
from utils.compute_lane_following_features import compute_lane_following_features

def parse_arguments() -> Any:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data_dir",
        default="",
        type=str,
        help="Directory where the sequences (csv files) are saved",
    )
    parser.add_argument(
        "--feature_dir",
        default="",
        type=str,
        help="Directory where the computed features are to be saved",
    )
    parser.add_argument("--mode",
                        required=True,
                        type=str,
                        help="train/val/test")
    parser.add_argument("--feature_type",
                        required=True,
                        type=str,
                        help="One of candidates_lanes/physics/semantic_map/lead_agent/lane_following (stored in config).",
                        choices=FEATURE_TYPES.keys())
    parser.add_argument(
        "--batch_size",
        default=100,
        type=int,
        help="Batch size for parallel computation",
    )
    parser.add_argument("--obs_len",
                        default=20,
                        type=int,
                        help="Observed length of the trajectory")
    parser.add_argument("--pred_len",
                        default=30,
                        type=int,
                        help="Prediction Horizon")
    parser.add_argument("--small",
                        action="store_true",
                        help="If true, a small subset of data is used.")
    parser.add_argument("--multi_agent",
                        default=False,
                        action="store_true",
                        help="If true, compute features will only compute the lane selection features.")
    return parser.parse_args()


def compute_features(
        seq_id: int,
        seq_path: str,
        map_features_utils_instance: MapFeaturesUtils,
        social_features_utils_instance: SocialFeaturesUtils,
        avm: ArgoverseMap,
        precomputed_lanes: pd.DataFrame,
        precomputed_physics: pd.DataFrame
) -> Tuple[list, list]:
    """Compute features for all.

    Args:
        seq_path (str): file path for the sequence whose features are to be computed.
        map_features_utils_instance: MapFeaturesUtils instance.
        social_features_utils_instance: SocialFeaturesUtils instance.
    Returns:
        columns (list of strings): ["SEQUENCE", "TRACK_ID", "FEATURE_1", ..., "FEATURE_N"]
        features_dataframe (pandas dataframe): Pandas dataframe where each cell is list of features or a list of features per centerline

    """
    args = parse_arguments()

    scene_df = pd.read_csv(seq_path, dtype={"TIMESTAMP": str})

    columns = list()
    all_feature_rows = list()

    # Compute agent list based on args.multi_agent
    agent_list = []
    if args.multi_agent:

        # Construct list of agents in the scene
        agent_list = scene_df["TRACK_ID"].unique().tolist()
    
    else:
        # Construct a list of only the Argo AGENT
        agent_list = scene_df[scene_df["OBJECT_TYPE"] == "AGENT"]["TRACK_ID"].unique().tolist()

    # Call function for the given feature type
    if args.feature_type == "testing": # Temp values for testing
        columns = ["SEQUENCE", "TRACK_ID", "MY_FEATURE"]
        all_feature_rows = [ [ seq_id, agent_list[0], 1.0 ], [ seq_id + 1, agent_list[0], 2.0 ]]

    elif args.feature_type == "candidate_lanes":
        columns, all_feature_rows = map_features_utils_instance.compute_lane_candidates(
            seq_id=seq_id,
            scene_df=scene_df,
            agent_list=agent_list,
            obs_len=args.obs_len,
            seq_len=args.obs_len + args.pred_len,
            raw_data_format=RAW_DATA_FORMAT,
            mode=args.mode,
            multi_agent=args.multi_agent,
            avm=avm
        )
 
    elif args.feature_type == "physics":
        columns, all_feature_rows = compute_physics_features(seq_path, seq_id)

    elif args.feature_type == "semantic_map":
        columns, all_feature_rows = compute_semantic_features(
            seq_id=seq_id,
            scene_df=scene_df,
            agent_list=agent_list,
            precomp_lanes = precomputed_lanes,
            raw_data_format = RAW_DATA_FORMAT,
            map_inst = avm
        )

    elif args.feature_type == "lead_agent":
        columns, all_feature_rows = map_features_utils_instance.compute_lead(
            seq_id=seq_id,
            scene_df=scene_df,
            agent_list=agent_list,
            obs_len=args.obs_len,
            seq_len=args.obs_len + args.pred_len,
            raw_data_format=RAW_DATA_FORMAT,
            mode=args.mode,
            multi_agent=args.multi_agent,
            avm=avm,
            precomputed_physics=precomputed_physics
        )

    elif args.feature_type == "lane_following":
        columns, all_feature_rows = compute_lane_following_features(
            seq_id=seq_id,
            scene_df=scene_df,
            obs_len=args.obs_len,
            agent_list=agent_list,
            precomputed_lanes=precomputed_lanes,
            raw_data_format=RAW_DATA_FORMAT,
            map_inst=avm,
            precomputed_physics=precomputed_physics
        )

    else:
        assert False, "Invalid feature type."


    return columns, all_feature_rows


def load_seq_save_features(
        start_idx: int,
        sequences: List[str],
        save_dir: str,
        map_features_utils_instance: MapFeaturesUtils,
        social_features_utils_instance: SocialFeaturesUtils,
        argoverse_map_api_instance: ArgoverseMap,
        precomputed_lanes: pd.DataFrame,
        precomputed_physics: pd.DataFrame
) -> None:
    """Load sequences, compute features, and save them.
    
    Args:
        start_idx : Starting index of the current batch
        sequences : Sequence file names
        save_dir: Directory where features for the current batch are to be saved
        map_features_utils_instance: MapFeaturesUtils instance
        social_features_utils_instance: SocialFeaturesUtils instance

    """
    args = parse_arguments()
    all_rows = []

    feature_columns, scene_rows = list(), dict()

    # Enumerate over the batch starting at start_idx
    for count, seq in enumerate(sequences[start_idx : start_idx + args.batch_size]):

        if not seq.endswith(".csv"):
            continue
        
        seq_file_path = f"{args.data_dir}/{seq}"
        seq_id = int(seq.split(".")[0])

        # Compute social and map features
        feature_columns, scene_rows = compute_features(
            seq_id, seq_file_path, map_features_utils_instance,
            social_features_utils_instance,
            argoverse_map_api_instance,
            precomputed_lanes,
            precomputed_physics)

        # Merge the features for all agents and all scenes
        all_rows.extend(scene_rows)

        if count % 200 == 0:
            print(
                f"count:{count}/total:{len(sequences)} with start {start_idx} and end {start_idx + args.batch_size}"
            )

    assert "SEQUENCE" in feature_columns, "Missing feature column: SEQUENCE"
    assert "TRACK_ID" in feature_columns, "Missing feature column: TRACK_ID"

    # Create dataframe for this batch
    data_df = pd.DataFrame(
        all_rows,
        columns=feature_columns,
    )

    # Save the ml feature data format
    if args.feature_type == "physics":
        save_ml_physics_features(all_rows, args.mode, seq_file_path, args.obs_len)

    # Save the computed features for all the sequences in the batch as a single file
    os.makedirs(save_dir, exist_ok=True)
    data_df.to_pickle(
        f"{save_dir}/forecasting_features_{args.mode}_{args.feature_type}_{start_idx}_{start_idx + args.batch_size}.pkl"
    )


def merge_saved_features(batch_save_dir: str) -> None:
    """Merge features saved by parallel jobs.

    Args:
        batch_save_dir: Directory where features for all the batches are saved.

    """
    args = parse_arguments()
    feature_files = os.listdir(batch_save_dir)
    all_features = []
    for feature_file in feature_files:
        if not feature_file.endswith(".pkl") or args.mode not in feature_file:
            continue
        file_path = f"{batch_save_dir}/{feature_file}"
        df = pd.read_pickle(file_path)
        all_features.append(df)

        # Remove the batch file
        os.remove(file_path)

    all_features_df = pd.concat(all_features, ignore_index=True)

    # Save the features for all the sequences into a single file
    all_features_df.to_pickle(
        f"{args.feature_dir}/forecasting_features_{args.mode}_{args.feature_type}.pkl")


def load_precomputed_features(args, feature_type):
    """Load the specified precomputed features."""

    feature_path = f"{args.feature_dir}/forecasting_features_{args.mode}_{feature_type}.pkl"
    print(f"Loading precomputed {feature_type}... {feature_path}")
    loaded_features = pkl.load( open( feature_path, "rb" ) )
    return pd.DataFrame(loaded_features)


if __name__ == "__main__":
    """Load sequences and save the computed features."""
    args = parse_arguments()

    start = time.time()

    # Warn if the data directory does not contain the mode
    if args.mode not in args.data_dir:
        print("WARNING: Mode does not match data directory name.")
    
    # Check if feature does not support multi-agent computation
    if args.multi_agent:
        # Check the feature supports multi agent
        assert FEATURE_TYPES[args.feature_type]["supports_multi_agent"], "This feature does not support computing for multiple agents in the scene."

    # Initialize Argoverse Map API and util functions
    map_features_utils_instance = MapFeaturesUtils()
    argoverse_map_api_instance = ArgoverseMap()
    social_features_utils_instance = None # SocialFeaturesUtils()

    # If required, load precomputed candidate lane pickle file
    precomputed_lanes = None
    if FEATURE_TYPES[args.feature_type]["uses_lanes"]:
        precomputed_lanes = load_precomputed_features(args, "candidate_lanes")

    # If required, load precomputed physics pickle files
    precomputed_physics = None
    if FEATURE_TYPES[args.feature_type]["uses_physics"]:
        precomputed_physics = load_precomputed_features(args, "physics")

    # Get list of scenes and create temp directory
    sequences = os.listdir(args.data_dir)
    temp_save_dir = tempfile.mkdtemp()

    # If the small flag is set, restrict the number os sequences (for testing)
    num_sequences = _FEATURES_SMALL_SIZE if args.small else len(sequences)

    # Compute features in parallel batches
    Parallel(n_jobs=-2)(delayed(load_seq_save_features)(
        i,
        sequences,
        temp_save_dir,
        map_features_utils_instance,
        social_features_utils_instance,
        argoverse_map_api_instance,
        precomputed_lanes,
        precomputed_physics
    ) for i in range(0, num_sequences, args.batch_size))

    # Switch the above parrallel call to this if visualizing with cProfile
    # load_seq_save_features(
    #     0,
    #     sequences,
    #     temp_save_dir,
    #     map_features_utils_instance,
    #     social_features_utils_instance,
    #     argoverse_map_api_instance
    # )

    # Merge the batched features and clean up
    merge_saved_features(temp_save_dir) 
    shutil.rmtree(temp_save_dir)

    print(
        f"Feature computation for {args.mode} set completed in {(time.time()-start)/60.0} mins"
    )
