import argparse
import logging
from typing import Any, List, Tuple, Union
import pickle

from gnomad.sample_qc.ancestry import assign_population_pcs, run_pca_with_relateds
from gnomad.utils.slack import slack_notifications
import hail as hl

from gnomad_qc.v4.resources.basics import get_checkpoint_path
from gnomad_qc.v4.resources.sample_qc import (
    ancestry_pca_eigenvalues,
    ancestry_pca_loadings,
    ancestry_pca_scores,
    get_pop_ht,
    get_qc_mt,
    joint_qc_meta,
    pca_related_samples_to_drop,
    pop_rf_path,
    pop_tsv_path,
)

logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger("ancestry_assignment")
logger.setLevel(logging.INFO)


def run_pca(
    remove_unreleasable_samples: bool,
    n_pcs: int,
    related_samples_to_drop: hl.Table,
    test: hl.bool = True,
) -> Tuple[List[float], hl.Table, hl.Table]:
    """
    Run population PCA using `run_pca_with_relateds`.

    :param remove_unreleasable_samples: Should unreleasable samples be removed the PCA
    :param n_pcs: Number of PCs to compute
    :param related_samples_to_drop: Table of related samples to drop from PCA run
    :param test: Subset qc mt to small test dataset.
    :return: eigenvalues, scores and loadings
    """
    logger.info("Running population PCA")
    qc_mt = get_qc_mt(test=test).mt()
    joint_meta = joint_qc_meta.ht()
    samples_to_drop = related_samples_to_drop.select()

    if remove_unreleasable_samples:
        logger.info("Excluding unreleasable samples for PCA.")
        samples_to_drop = samples_to_drop.union(
            qc_mt.filter_cols(
                (~joint_meta[qc_mt.col_key].releasable)
                | (
                    ~joint_meta[qc_mt.col_key].v3_meta.v3_release
                )  # TODO: Switch back to releasable? Need to discuss with Julia about adding to joint meta
            )
            .cols()
            .select()
        )
    else:
        logger.info("Including unreleasable samples for PCA")

    return run_pca_with_relateds(qc_mt, samples_to_drop, n_pcs=n_pcs)


def calculate_mislabeled_training(pop_ht: hl.Table, pop_field: str) -> [int, float]:
    """
    Calculate the number and proportion of mislabeled training samples.

    :param pop_ht: Table with assigned pops/subpops that is returned by `assign_population_pcs`
    :param pop_field: Name of field in the Table containing the assigned pop/subpop
    :return: The number and proportion of mislabeled training samples
    """
    n_mislabeled_samples = pop_ht.aggregate(
        hl.agg.count_where(pop_ht.training_pop != pop_ht[pop_field])
    )

    defined_training_pops = pop_ht.aggregate(
        hl.agg.count_where(hl.is_defined(pop_ht.training_pop))
    )

    prop_mislabeled_samples = n_mislabeled_samples / defined_training_pops

    return n_mislabeled_samples, prop_mislabeled_samples


def prep_ht_for_rf(
    remove_unreleasable_samples: bool = False,
    withhold_prop: hl.float = None,
    seed: int = 24,
    test: bool = False,
) -> hl.Table:
    """
    Prepare the pca scores hail Table for the random forest population assignment runs.

    :param remove_unreleasable_samples: Should unreleasable samples be remove in the PCA.
    :param withhold_prop: Proportion of training pop samples to withhold from training will keep all samples if `None`.
    :param seed: Random seed, defaults to 24.
    :param test: Whether RF is running on test qc mt.
    :return Table with input for the random forest.
    """
    pop_pca_scores_ht = ancestry_pca_scores(remove_unreleasable_samples, test).ht()
    joint_meta = joint_qc_meta.ht()[pop_pca_scores_ht.key]

    # Assign known populations or prevoiusly inferred pops as training pop for the RF
    pop_pca_scores_ht = pop_pca_scores_ht.annotate(
        training_pop=(
            hl.case()
            .when(
                hl.is_defined(joint_meta.v3_meta.v3_project_pop),
                joint_meta.v3_meta.v3_project_pop,
            )
            .when(
                joint_meta.v3_meta.v3_population_inference.pop != "oth",
                joint_meta.v3_meta.v3_population_inference.pop,
            )  # TODO: Anylsis of where v2_pop does not agree with v3 assigned pop
            .or_missing()
        ),
        hgdp_or_tgp=joint_meta.v3_meta.v3_subsets.hgdp
        | joint_meta.v3_meta.v3_subsets.tgp,
    )
    # Keep track of original training population labels, this is useful if samples are withheld to create PR curves
    pop_pca_scores_ht = pop_pca_scores_ht.annotate(
        original_training_pop=pop_pca_scores_ht.training_pop,
    )

    # Use the withhold proportion to create PR curves for when the RF removes samples because it will remove samples that are misclassified
    # TODO: talk to kristen about how she handled this for subpops
    if withhold_prop:
        pop_pca_scores_ht = pop_pca_scores_ht.annotate(
            training_pop=hl.or_missing(
                hl.is_defined(pop_pca_scores_ht.training_pop)
                & hl.rand_bool(1.0 - withhold_prop, seed=seed),
                pop_pca_scores_ht.training_pop,
            )
        )

        pop_pca_scores_ht = pop_pca_scores_ht.annotate(
            withheld_sample=hl.is_defined(pop_pca_scores_ht.original_training_pop)
            & (~hl.is_defined(pop_pca_scores_ht.training_pop))
        )
    return pop_pca_scores_ht


def assign_pops(
    min_prob: float,
    remove_unreleasable_samples: bool = False,
    max_number_mislabeled_training_samples: int = None,
    max_proportion_mislabeled_training_samples: float = None,
    pcs: List[int] = list(range(1, 17)),
    withhold_prop: float = None,
    missing_label: str = "unassigned",  # TODO: Bring up at project meeting to decide
    seed: int = 24,
    test: bool = False,
    overwrite: bool = False,
) -> Tuple[hl.Table, Any]:
    """
    Use a random forest model to assign global population labels based on the results from `run_pca`.

    The training data used is v3 project metadata's `project_pop` if it is defined, otherwise it uses `v2_pop` if defined with the exception of `oth`.
    The method starts by inferring the pop on all samples, then comparing the training data to the inferred data,
    removing the truth outliers and re-training. This is repeated until the number of truth samples is less than
    or equal to `max_number_mislabeled_training_samples` or `max_proportion_mislabeled_training_samples`. Only one
    of `max_number_mislabeled_training_samples` or `max_proportion_mislabeled_training_samples` can be set.

    :param min_prob: Minimum RF probability for pop assignment
    :param remove_unreleasable_samples: Whether unreleasable were removed from PCA.
    :param max_number_mislabeled_training_samples: Maximum number of training samples that can be mislabelled
    :param max_proportion_mislabeled_training_samples: Maximum proportion of training samples that can be mislabelled
    :param pcs: List of PCs to use in the RF
    :param withhold_prop: Proportion of training pop samples to withhold from training will keep all samples if `None`
    :param missing_label: Label for samples for which the assignment probability is smaller than `min_prob`
    :param seed: Random seed, defaults to 24.
    :param test: Whether running assigment on a test dataset.
    :param overwrite: Whether to overwrite existing files.
    :return: Table of pop assignments and the RF model
    """
    logger.info("Assigning global population labels")

    logger.info("Checking passed args...")
    if (
        max_number_mislabeled_training_samples is not None
        and max_proportion_mislabeled_training_samples is not None
    ):
        raise ValueError(
            "One and only one of max_number_mislabeled_training_samples or max_proportion_mislabeled_training_samples must be set!"
        )
    elif max_proportion_mislabeled_training_samples is not None:
        max_mislabeled = max_proportion_mislabeled_training_samples
    else:
        max_mislabeled = max_number_mislabeled_training_samples

    pop_pca_scores_ht = prep_ht_for_rf(
        remove_unreleasable_samples, withhold_prop, seed, test
    )
    pop_field = "pop"
    logger.info(
        "Running RF using %d training examples",
        pop_pca_scores_ht.aggregate(
            hl.agg.count_where(hl.is_defined(pop_pca_scores_ht.training_pop))
        ),
    )
    # Run the pop RF for the first time
    pop_ht, pops_rf_model = assign_population_pcs(
        pop_pca_scores_ht,
        pc_cols=pcs,
        known_col="training_pop",
        output_col=pop_field,
        min_prob=min_prob,
        missing_label=missing_label,
    )

    # Calculate number and proportion of mislabeled samples
    n_mislabeled_samples, prop_mislabeled_samples = calculate_mislabeled_training(
        pop_ht, pop_field
    )

    mislabeled = (
        n_mislabeled_samples
        if max_number_mislabeled_training_samples
        else prop_mislabeled_samples  # TODO: Run analysis on what makes sense as input here, utilize withhold arg for PR curves
    )
    pop_assignment_iter = 1

    # Rerun the RF until the number of mislabeled samples (known pop != assigned pop) is below our max mislabeled threshold
    while mislabeled > max_mislabeled:
        pop_assignment_iter += 1
        logger.info(
            "Found %i(%d%%) training samples labeled differently from their known pop. Re-running assignment without them.",
            n_mislabeled_samples,
            round(prop_mislabeled_samples * 100, 2),
        )

        pop_ht = pop_ht[pop_pca_scores_ht.key]

        # Remove mislabled samples from RF training unless they are from 1KG or HGDP
        pop_pca_scores_ht = pop_pca_scores_ht.annotate(
            training_pop=hl.or_missing(
                (
                    (pop_ht.training_pop == pop_ht[pop_field])
                    | (pop_pca_scores_ht.hgdp_or_tgp)
                ),
                pop_pca_scores_ht.training_pop,
            ),
        )
        pop_pca_scores_ht = pop_pca_scores_ht.checkpoint(
            get_checkpoint_path(
                f"assign_pops_rf_iter_{pop_assignment_iter}{'_test' if test else ''}"
            ),
            overwrite=overwrite,
        )

        logger.info(
            "Running RF using %d training examples",
            pop_pca_scores_ht.aggregate(
                hl.agg.count_where(hl.is_defined(pop_pca_scores_ht.training_pop))
            ),
        )
        pop_ht, pops_rf_model = assign_population_pcs(
            pop_pca_scores_ht,
            pc_cols=pcs,
            known_col="training_pop",
            output_col=pop_field,
            min_prob=min_prob,
            missing_label=missing_label,
        )

        n_mislabeled_samples, prop_mislabeled_samples = calculate_mislabeled_training(
            pop_ht, pop_field
        )

        mislabeled = (
            n_mislabeled_samples
            if max_number_mislabeled_training_samples
            else prop_mislabeled_samples  # TODO: Run analysis on what makes sense as input here, utilize withhold arg for PR curves
        )
        logger.info("%d mislabeled samples.", mislabeled)

    pop_ht = pop_ht.annotate(
        original_training_pop=pop_pca_scores_ht[pop_ht.key].original_training_pop
    )
    pop_ht = pop_ht.annotate_globals(
        min_prob=min_prob,
        remove_unreleasable_samples=remove_unreleasable_samples,
        max_mislabeled=max_mislabeled,
        pop_assignment_iterations=pop_assignment_iter,
        pcs=pcs,
        n_mislabeled_training_samples=n_mislabeled_samples,
        prop_mislabeled_training_samples=prop_mislabeled_samples,
    )
    if withhold_prop:
        pop_ht = pop_ht.annotate_globals(withhold_prop=withhold_prop)
        pop_ht = pop_ht.annotate(
            withheld_sample=pop_pca_scores_ht[pop_ht.key].withheld_sample
        )

    return pop_ht, pops_rf_model


def write_pca_results(
    pop_pca_eigenvalues: List[float],
    pop_pca_scores_ht: hl.Table,
    pop_pca_loadings_ht: hl.Table,
    overwrite: hl.bool,
    removed_unreleasables: hl.bool,
    test: hl.bool = False,
):
    """
    Write out the three objects returned by run_pca().

    :param pop_pca_eigenvalues: List of eigenvalues returned by run_pca.
    :param pop_pca_scores_ht: Table of scores returned by run_pca.
    :param pop_pca_loadings_ht: Table of loadings returned by run_pca.
    :param overwrite: Whether to overwrite an existing file.
    :param removed_unreleasables: Whether run_pca removed unreleasable samples.
    :param test: Whether the test qc mt was used in pca.
    :return: None
    """
    pop_pca_eigenvalues_ht = hl.Table.parallelize(
        hl.literal(
            [{"PC": i + 1, "eigenvalue": x} for i, x in enumerate(pop_pca_eigenvalues)],
            "array<struct{PC: int, eigenvalue: float}>",
        )
    )
    pop_pca_eigenvalues_ht.write(
        ancestry_pca_eigenvalues(removed_unreleasables, test).path, overwrite=overwrite,
    )
    pop_pca_scores_ht.write(
        ancestry_pca_scores(removed_unreleasables, test).path, overwrite=overwrite,
    )
    pop_pca_loadings_ht.write(
        ancestry_pca_loadings(removed_unreleasables, test).path, overwrite=overwrite,
    )


def main(args):
    hl.init(
        log="/assign_ancestry.log",
        default_reference="GRCh38",
        tmp_dir="gs://gnomad-tmp-4day",
    )

    remove_unreleasables = args.remove_unreleasable_samples
    overwrite = args.overwrite
    test = args.test

    if args.run_pca:
        pop_eigenvalues, pop_scores_ht, pop_loadings_ht = run_pca(
            remove_unreleasables, args.n_pcs, pca_related_samples_to_drop.ht(), test,
        )

        write_pca_results(
            pop_eigenvalues,
            pop_scores_ht,
            pop_loadings_ht,
            overwrite,
            remove_unreleasables,
            test,
        )

    if args.assign_pops:
        pop_pcs = args.pop_pcs
        pop_ht, pops_rf_model = assign_pops(
            args.min_pop_prob,
            remove_unreleasables,
            max_number_mislabeled_training_samples=args.max_number_mislabeled_training_samples,
            max_proportion_mislabeled_training_samples=args.max_proportion_mislabeled_training_samples,
            pcs=pop_pcs,
            withhold_prop=args.withhold_prop,
            test=test,
            overwrite=overwrite,
        )
        pop_ht = pop_ht.checkpoint(
            get_pop_ht(test=test).path,
            overwrite=overwrite,
            _read_if_exists=not overwrite,
        )
        pop_ht.transmute(
            **{f"PC{i}": pop_ht.pca_scores[i - 1] for i in pop_pcs}
        ).export(pop_tsv_path(test=test))

        with hl.hadoop_open(pop_rf_path(test=test), "wb") as out:
            pickle.dump(pops_rf_model, out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-pca", help="Compute PCA", action="store_true")
    parser.add_argument(
        "--n-pcs",
        help="Number of PCs to compute for ancestry PCA. Defaults to 30.",
        default=30,
        type=int,
    )
    parser.add_argument(
        "--remove-unreleasable-samples",
        help="Remove unreleasable samples when computing PCA.",
        action="store_true",
    )
    parser.add_argument(
        "--assign-pops", help="Assigns pops from PCA.", action="store_true"
    )
    parser.add_argument(
        "--pop-pcs",
        help="List of PCs to use for ancestry assignment. The values provided should be 1-based. Defaults to 16 PCs",
        default=list(range(1, 17)),
        type=list,
    )
    parser.add_argument(
        "--min-pop-prob",
        help="Minimum RF prob for pop assignment. Defaults to 0.75.",
        default=0.75,
        type=float,
    )
    parser.add_argument(
        "--withhold-prop",
        help="Proportion of training samples to withhold from pop assignment RF training.",
        type=float,
    )
    mislabel_parser = parser.add_mutually_exclusive_group(required=True)
    mislabel_parser.add_argument(
        "--max-number-mislabeled-training-samples",
        help="Maximum number of training samples that can be mislabelled. Can't be used if `max-proportion-mislabeled-training-samples` is already set",
        type=int,
        default=None,
    )
    mislabel_parser.add_argument(
        "--max-proportion-mislabeled-training-samples",
        help="Maximum proportion of training samples that can be mislabelled. Can't be used if `max-number-mislabeled-training-samples` is already set",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--slack-channel", help="Slack channel to post results and notifications to.",
    )
    parser.add_argument(
        "--test", help="Run script on test dataset.", action="store_true"
    )
    parser.add_argument(
        "--overwrite", help="Overwrite output files.", action="store_true"
    )

    args = parser.parse_args()

    if args.slack_channel:
        from gnomad_qc.slack_creds import slack_token

        with slack_notifications(slack_token, args.slack_channel):
            main(args)
    else:
        main(args)
