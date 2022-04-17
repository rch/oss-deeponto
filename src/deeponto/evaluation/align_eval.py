# Copyright 2021 Yuan He (KRR-Oxford). All rights reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Script for evaluating implemented ontology matching models."""

import multiprocessing_on_dill as mt

from deeponto import SavedObj
from deeponto.onto.mapping import OntoMappings
from deeponto.utils import evenly_divide, detect_path, uniqify
from deeponto.utils.logging import banner_msg
from deeponto.evaluation.eval_metrics import *


def pred_thresholding(pred_maps: OntoMappings, threshold: float):

    # load all prediction mappings from the saved directory
    filtered_pred = pred_maps.topKs(threshold, K=pred_maps.n_best)
    if pred_maps.flag == "tgt2src":
        # reverse (head, tail) to match src2tgt
        filtered_pred = [(y, x) for (x, y) in filtered_pred]

    return filtered_pred


def global_match_eval(
    pred_path: Optional[str],
    ref_path: str,
    null_ref_path: Optional[str],
    threshold: float,
    processed_pred: Optional[List[Tuple[str, str]]] = None,
    flag: str = "",
    show_more_f_scores: bool = False,
):
    """Eval on Precision, Recall, and F-score (most general OM eval)
    """

    banner_str = f"Eval using P, R, F-score with threshold: {threshold}"
    if flag:
        banner_str += f" ({flag})"
    banner_msg(banner_str)

    # load prediction mappings from the saved directory
    if not processed_pred:
        pred_maps = OntoMappings.from_saved(pred_path)
        pred = pred_thresholding(pred_maps, threshold)
    else:
        pred = processed_pred

    # load reference mappings and (opt) null mappings
    ref = OntoMappings.read_tsv_mappings(ref_path).to_tuples()
    null_ref = OntoMappings.read_tsv_mappings(null_ref_path).to_tuples() if null_ref_path else None

    results = f1(pred, ref, null_ref)
    if show_more_f_scores:
        results_favour_recall = f_score(pred, ref, beta=2, null_ref=null_ref)
        results_favour_precision = f_score(pred, ref, beta=0.5, null_ref=null_ref)
        results["f_2"] = results_favour_recall["f_score"]
        results["f_0.5"] = results_favour_precision["f_score"]
    SavedObj.print_json(results)

    return results


def global_match_select(
    global_match_dir: str,
    train_ref_path: Optional[str],
    val_ref_path: str,
    test_ref_path: Optional[str],
    null_ref_path: Optional[str],
    num_procs: int = 10,
):

    # grid search the following mapping thresholds and mappings
    thresholds = (
        evenly_divide(0, 0.8, 8) + evenly_divide(0.9, 0.97, 7) + evenly_divide(0.98, 0.999, 19)
    )
    mapping_types = ["src2tgt", "tgt2src", "combined"]

    # load src2tgt and tgt2src mappings
    src2tgt_pred_maps = OntoMappings.from_saved(global_match_dir + "/src2tgt")
    tgt2src_pred_maps = OntoMappings.from_saved(global_match_dir + "/tgt2src")
    pred_batches = []
    for threshold in thresholds:
        threshold = round(threshold, 6)
        src2tgt_pred = pred_thresholding(src2tgt_pred_maps, threshold)
        tgt2src_pred = pred_thresholding(tgt2src_pred_maps, threshold)
        combined_pred = uniqify(src2tgt_pred + tgt2src_pred)
        pred_batches.append((src2tgt_pred, tgt2src_pred, combined_pred, threshold))

    # merge the reference mappings with the null reference mappings because
    # they should be ignored in hyperparam selection
    merged_null_ref_path = global_match_dir + "/null_ref_for_model_select.tsv"
    if not detect_path(merged_null_ref_path):
        train_ref = (
            OntoMappings.read_tsv_mappings(train_ref_path).to_tuples() if null_ref_path else []
        )
        null_ref = (
            OntoMappings.read_tsv_mappings(null_ref_path).to_tuples() if null_ref_path else []
        )
        test_ref = OntoMappings.read_tsv_mappings(test_ref_path).to_tuples()

        null_ref = (
            null_ref + train_ref + test_ref
        )  # only val ref is not ignored during hyperparam selection
        with open(merged_null_ref_path, "w+") as f:
            f.write("SrcEntity\tTgtEntity\tScore\n")
            for src_ent, tgt_ent in null_ref:
                f.write(f"{src_ent}\t{tgt_ent}\t1.0\n")

    # invoke 10 threads for faster hyperparam selection
    pool = mt.Pool(num_procs)
    eval_results = dict()

    for src2tgt, tgt2src, combined, thr in pred_batches:
        eval_results[thr] = dict()
        preds = {"src2tgt": src2tgt, "tgt2src": tgt2src, "combined": combined}
        for flag in mapping_types:
            eval_results[thr][flag] = pool.apply_async(
                global_match_eval,
                args=(
                    None,
                    val_ref_path,
                    merged_null_ref_path,
                    thr,
                    preds[flag],
                    flag,
                ),
            )
    pool.close()
    pool.join()

    best_results = {
        "threshold": 0.0,
        "map_type": None,
        "best_f1": 0.0,
    }
    serialized_eval_results = dict()
    for thr, results in eval_results.items():
        serialized_eval_results[thr] = dict()
        for map_type, scores in results.items():
            scores = scores.get()
            serialized_eval_results[thr][map_type] = scores
            if scores["f_score"] >= best_results["best_f1"]:
                best_results["threshold"] = thr
                best_results["map_type"] = map_type
                best_results["best_f1"] = scores["f_score"]

    banner_msg("Best Hyperparameters for Validation")
    SavedObj.save_json(serialized_eval_results, global_match_dir + "/results.val.json")
    SavedObj.print_json(best_results)
    SavedObj.save_json(best_results, global_match_dir + "/best_hyperparams.val.json")


def local_rank_eval(pred_path: str, ref_anchor_path: str, ref_path: str, *ks: int):
    """Eval on Hits@K, MRR (estimating OM performance) 
    """

    banner_msg("Eval using Hits@K, MRR")

    # load prediction mappings from the saved directory
    pred_maps = OntoMappings.from_saved(pred_path)
    ref_anchor_maps = AnchoredOntoMappings.from_saved(ref_anchor_path)
    ref_anchor_maps.fill_scored_maps(pred_maps)
    # print(ref_anchor_maps.anchor2cand)

    # load reference mappings and (opt) null mappings
    ref = OntoMappings.read_tsv_mappings(ref_path, 0.0).to_tuples()

    results = dict()
    results["MRR"] = round(mean_reciprocal_rank(ref_anchor_maps, ref), 3)
    for k in ks:
        results[f"Hits@{k}"] = round(hits_at_k(ref_anchor_maps, ref, k), 3)
    SavedObj.print_json(results)

    return results
