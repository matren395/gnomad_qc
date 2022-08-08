import argparse
import logging

import hail as hl

from gnomad.resources.grch38.reference_data import telomeres_and_centromeres
from gnomad.sample_qc.filtering import compute_stratified_sample_qc
from gnomad.utils.annotations import bi_allelic_expr
from gnomad.utils.filtering import add_filters_expr, filter_to_adj
from gnomad.utils.slack import slack_notifications

from gnomad_qc.slack_creds import slack_token
from gnomad_qc.v4.resources.basics import (
    calling_intervals,
    get_checkpoint_path,
    get_gnomad_v4_vds,
    get_logging_path,
    gnomad_v4_testset,
    gnomad_v4_testset_meta,
)
from gnomad_qc.v4.resources.meta import project_meta
from gnomad_qc.v4.resources.sample_qc import (
    fingerprinting_failed,
    get_sample_qc,
    hard_filtered_samples,
    hard_filtered_samples_no_sex,
    interval_coverage,
    sex,
    v4_predetermined_qc,
)

logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger("hard_filters")
logger.setLevel(logging.INFO)


def compute_sample_qc(n_partitions: int = 500, test: bool = False) -> hl.Table:
    """
    Perform sample QC on the raw split matrix table using `compute_stratified_sample_qc`.

    :param n_partitions: Number of partitions to write the output sample QC HT to.
    :param test: Whether to use the gnomAD v4 test dataset. Default is to use the full dataset.
    :return: Table containing sample QC metrics.
    """
    logger.info("Computing sample QC")
    vds = get_gnomad_v4_vds(split=True, remove_hard_filtered_samples=False, test=test)
    vds = hl.vds.filter_chromosomes(vds, keep_autosomes=True)

    # Remove centromeres and telomeres in case they were included
    vds = hl.vds.filter_intervals(
        vds, intervals=telomeres_and_centromeres.ht(), keep=False
    )

    sample_qc_ht = compute_stratified_sample_qc(
        vds,
        strata={
            "bi_allelic": bi_allelic_expr(vds.variant_data),
            "multi_allelic": ~bi_allelic_expr(vds.variant_data),
        },
        tmp_ht_prefix=get_sample_qc(test=test).path[:-3],
        gt_col="GT",
    )

    return sample_qc_ht.repartition(n_partitions)


def compute_hard_filters(
    include_sex_filter: bool = False,
    max_n_singleton: float = 5000,
    max_r_het_hom_var: float = 10,
    min_bases_dp_over_1: float = 5e7,
    min_bases_dp_over_20: float = 4e7,
    max_contamination: float = 0.05,
    max_chimera: float = 0.05,
    test: bool = False,
    coverage_mt: hl.MatrixTable = None,
    min_cov: int = None,
    min_qc_mt_adj_callrate: int = None,
) -> hl.Table:
    """
    Apply hard filters to samples and return a Table with the filtered samples and the reason for filtering.

    If `include_sex_filter` is True, this function expects a sex inference Table generated by
    `sex_inference.py --impute-sex`.

    .. warning::
        The defaults used in this function are callset specific, these hardfilter cutoffs will need to be re-examined
        for each callset

    :param include_sex_filter: If sex inference should be used in filtering.
    :param max_n_singleton: Filtering threshold to use for the maximum number of singletons.
    :param max_r_het_hom_var: Filtering threshold to use for the maximum ratio of heterozygotes to alternate homozygotes.
    :param min_bases_dp_over_1: Filtering threshold to use for the minimum number of bases with a DP over one.
    :param min_bases_dp_over_20: Filtering threshold to use for the minimum number of bases with a DP over 20.
    :param max_contamination: Filtering threshold to use for maximum contamination (this is a proportion not a
        percent, e.g. 5% == 0.05, %5 != 5).
    :param max_chimera: Filtering threshold to use for maximum chimera (this is a proportion not a percent,
        e.g. 5% == 0.05, %5 != 5).
    :param test: Whether to use the gnomAD v4 test dataset. Default is to use the full dataset.
    :param coverage_mt: MatrixTable containing the per interval per sample coverage statistics.
    :param min_cov: Filtering threshold to use for chr20 coverage.
    :param min_qc_mt_adj_callrate: Filtering threshold to use for sample callrate computed on only predetermined QC
        variants (predetermined using CCDG genomes/exomes, gnomAD v3.1 genomes, and UKBB exomes) after ADJ filtering.
    :return: Table of hard filtered samples.
    """
    ht = get_gnomad_v4_vds(
        remove_hard_filtered_samples=False, test=test
    ).variant_data.cols()
    ht = ht.annotate_globals(
        hard_filter_cutoffs=hl.struct(
            max_n_singleton=max_n_singleton,
            max_r_het_hom_var=max_r_het_hom_var,
            min_bases_dp_over_1=min_bases_dp_over_1,
            min_bases_dp_over_20=min_bases_dp_over_20,
            max_contamination=max_contamination,
            max_chimera=max_chimera,
        ),
    )
    if min_cov is not None:
        ht = ht.annotate_globals(
            hard_filter_cutoffs=ht.hard_filter_cutoffs.annotate(
                chr_20_dp_threshold=min_cov
            )
        )

    hard_filters = dict()
    sample_qc_metric_hard_filters = dict()

    # Flag samples failing fingerprinting
    fp_ht = fingerprinting_failed.ht()
    hard_filters["failed_fingerprinting"] = hl.is_defined(fp_ht[ht.key])

    # Flag extreme raw bi-allelic sample QC outliers
    bi_allelic_qc_ht = get_sample_qc("bi_allelic", test=test).ht()
    # Convert tuples to lists so we can find the index of the passed threshold
    bi_allelic_qc_ht = bi_allelic_qc_ht.annotate(
        **{
            f"bases_dp_over_{hl.eval(bi_allelic_qc_ht.dp_bins[i])}": bi_allelic_qc_ht.bases_over_dp_threshold[
                i
            ]
            for i in range(len(bi_allelic_qc_ht.dp_bins))
        },
    )
    bi_allelic_qc_struct = bi_allelic_qc_ht[ht.key]
    sample_qc_metric_hard_filters["high_n_singleton"] = (
        bi_allelic_qc_struct.n_singleton > max_n_singleton
    )
    sample_qc_metric_hard_filters["high_r_het_hom_var"] = (
        bi_allelic_qc_struct.r_het_hom_var > max_r_het_hom_var
    )
    sample_qc_metric_hard_filters["low_bases_dp_over_1"] = (
        bi_allelic_qc_struct.bases_dp_over_1 < min_bases_dp_over_1
    )
    sample_qc_metric_hard_filters["low_bases_dp_over_20"] = (
        bi_allelic_qc_struct.bases_dp_over_20 < min_bases_dp_over_20
    )
    hard_filters["sample_qc_metrics"] = (
        sample_qc_metric_hard_filters["high_n_singleton"]
        | sample_qc_metric_hard_filters["high_r_het_hom_var"]
        | sample_qc_metric_hard_filters["low_bases_dp_over_1"] |
        sample_qc_metric_hard_filters["low_bases_dp_over_20"]
    )

    # Flag samples that fail bam metric thresholds
    if test:
        project_meta_ht = gnomad_v4_testset_meta.ht()
        # Use the gnomAD v4 test dataset's `rand_sampling_meta` annotation to get the bam metrics needed for hard filtering
        # This annotation includes all of the metadata for the random samples chosen for the test dataset
        bam_metrics_struct = project_meta_ht[ht.key].rand_sampling_meta
        bam_metrics_struct = bam_metrics_struct.annotate(
            contam_rate=bam_metrics_struct.freemix,
            chimeras_rate=bam_metrics_struct.pct_chimeras,
        )
    else:
        project_meta_ht = project_meta.ht()
        bam_metrics_struct = project_meta_ht[ht.key].bam_metrics

    hard_filters["contamination"] = bam_metrics_struct.contam_rate > max_contamination
    hard_filters["chimera"] = bam_metrics_struct.chimeras_rate > max_chimera

    # Flag low-coverage samples using mean coverage on chromosome 20
    if min_cov is not None:
        if coverage_mt is None:
            raise ValueError(
                "If a chromosome 20 coverage threshold is supplied, a coverage MatrixTable must be supplied too."
            )
        coverage_mt = coverage_mt.filter_rows(
            coverage_mt.interval.start.contig == "chr20"
        )
        coverage_ht = coverage_mt.select_cols(
            chr20_mean_dp=hl.agg.sum(coverage_mt.sum_dp)
            / hl.agg.sum(coverage_mt.interval_size)
        ).cols()
        hard_filters["low_coverage"] = coverage_ht[ht.key].chr20_mean_dp < min_cov

    if min_qc_mt_adj_callrate is not None:
        mt = v4_predetermined_qc.mt()
        num_samples = mt.count_rows()
        # Filter predetermined QC variants to only variants with a high pre ADJ callrate
        mt = mt.filter_rows((hl.agg.count_where(hl.is_defined(mt.GT)) / num_samples) > 0.99)
        num_variants = mt.count_rows()
        mt = filter_to_adj(mt)
        callrate_ht = mt.annotate_cols(
            callrate_adj=hl.agg.count_where(hl.is_defined(mt.GT)) / num_variants
        ).cols()
        hard_filters["low_adj_callrate"] = callrate_ht[ht.key].callrate_adj < min_qc_mt_adj_callrate

    if include_sex_filter:
        sex_struct = sex.ht()[ht.key]
        # Remove samples with ambiguous sex assignments
        hard_filters["ambiguous_sex"] = sex_struct.sex_karyotype == "ambiguous"
        hard_filters[
            "sex_aneuploidy"
        ] = ~hl.set(  # pylint: disable=invalid-unary-operand-type
            {"ambiguous", "XX", "XY"}
        ).contains(
            sex_struct.sex_karyotype
        )

    ht = ht.annotate(
        hard_filters=add_filters_expr(filters=hard_filters),
        sample_qc_metric_hard_filters=add_filters_expr(
            filters=sample_qc_metric_hard_filters
        ),
    )

    # Keep samples failing hard filters
    ht = ht.filter(hl.len(ht.hard_filters) > 0)
    return ht


def main(args):
    hl.init(
        log="/gnomad_hard_filters.log",
        default_reference="GRCh38",
        tmp_dir="gs://gnomad-tmp-4day",
    )
    # NOTE: remove this flag when the new shuffle method is the default
    hl._set_flags(use_new_shuffle="1")

    calling_interval_name = args.calling_interval_name
    calling_interval_padding = args.calling_interval_padding
    test = args.test

    try:
        if args.sample_qc:
            compute_sample_qc(
                n_partitions=args.sample_qc_n_partitions, test=test
            ).write(get_sample_qc(test=test).path, overwrite=args.overwrite)

        if args.compute_coverage:
            if test:
                logger.info("Loading test VDS...")
                vds = gnomad_v4_testset.vds()
            else:
                logger.info("Loading full v4 VDS...")
                vds = get_gnomad_v4_vds(remove_hard_filtered_samples=False)

            logger.info(
                "Loading calling intervals: %s with padding of %d...",
                calling_interval_name,
                calling_interval_padding,
            )
            ht = calling_intervals(calling_interval_name, calling_interval_padding).ht()
            mt = hl.vds.interval_coverage(vds, intervals=ht)
            mt = mt.annotate_globals(
                calling_interval_name=calling_interval_name,
                calling_interval_padding=calling_interval_padding,
            )
            mt.write(
                get_checkpoint_path(
                    f"test_interval_coverage.{calling_interval_name}.pad{calling_interval_padding}",
                    mt=True,
                )
                if test
                else interval_coverage.path,
                overwrite=args.overwrite,
            )

        if args.compute_hard_filters:
            # TODO: Determine cutoffs by visual inspection of the metrics, and modify defaults to match
            if test:
                coverage_mt = hl.read_matrix_table(
                    get_checkpoint_path(
                        f"test_interval_coverage.{calling_interval_name}.pad{calling_interval_padding}",
                        mt=True,
                    )
                )
            else:
                coverage_mt = interval_coverage.mt()

            if args.include_sex_filter:
                hard_filter_path = hard_filtered_samples.path
                if test:
                    hard_filter_path = get_checkpoint_path(
                        "test_gnomad.exomes.hard_filtered_samples"
                    )
            else:
                hard_filter_path = hard_filtered_samples_no_sex.path
                if test:
                    hard_filter_path = get_checkpoint_path(
                        "test_gnomad.exomes.hard_filtered_samples_no_sex"
                    )

            ht = compute_hard_filters(
                args.include_sex_filter,
                args.max_n_singleton,
                args.max_r_het_hom_var,
                args.min_bases_dp_over_1,
                args.min_bases_dp_over_20,
                args.max_contamination,
                args.max_chimera,
                test,
                coverage_mt,
                args.min_cov,
                args.min_qc_mt_adj_callrate,
            )
            ht = ht.checkpoint(hard_filter_path, overwrite=args.overwrite)
            ht.group_by("hard_filters").aggregate(n=hl.agg.count()).show(20)
            ht.group_by("sample_qc_metric_hard_filters").aggregate(
                n=hl.agg.count()
            ).show(20)
    finally:
        logger.info("Copying log to logging bucket...")
        hl.copy_log(get_logging_path("hard_filters"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--overwrite",
        help="Overwrite all Matrixtables/Tables. (default: False).",
        action="store_true",
    )
    parser.add_argument(
        "--test",
        help="Use the v4 test dataset instead of the full dataset.",
        action="store_true",
    )
    parser.add_argument(
        "--sample-qc", help="Compute Hail's VDS sample QC metrics.", action="store_true"
    )
    parser.add_argument(
        "--sample-qc-n-partitions",
        help="Number of desired partitions for the sample QC output Table.",
        default=500,
        type=int,
    )
    parser.add_argument(
        "--compute-coverage",
        help="Compute per interval coverage metrics using Hail's vds.interval_coverage method.",
        action="store_true",
    )
    parser.add_argument(
        "--calling-interval-name",
        help="Name of calling intervals to use for interval coverage. One of: 'ukb', 'broad', or 'intersection'.",
        type=str,
        choices=["ukb", "broad", "intersection"],
        default="intersection",
    )
    parser.add_argument(
        "--calling-interval-padding",
        help="Number of base pair padding to use on the calling intervals. One of 0 or 50 bp.",
        type=int,
        choices=[0, 50],
        default=50,
    )
    parser.add_argument(
        "--compute-hard-filters",
        help="Computes samples to be hard-filtered. NOTE: Cutoffs should be determined by visual inspection of the metrics.",
        action="store_true",
    )
    parser.add_argument(
        "--include-sex-filter",
        help="If sex filters should be included in hard filtering.",
        action="store_true",
    )
    parser.add_argument_group()
    hard_filter_args = parser.add_argument_group(
        "Hard-filter cutoffs", "Arguments used for hard-filter cutoffs."
    )
    hard_filter_args.add_argument(
        "--max-n-singleton",
        type=float,
        default=5000,
        help="Filtering threshold to use for the maximum number of singletons. Default is 5000.",
    )
    hard_filter_args.add_argument(
        "--max-r-het-hom-var",
        type=float,
        default=10,
        help="Filtering threshold to use for the maximum ratio of heterozygotes to alternate homozygotes. Default is 10.",
    )
    hard_filter_args.add_argument(
        "--min-bases-dp-over-1",
        type=float,
        help="Filtering threshold to use for the minimum number of bases with a DP over one. Default is 5e7.",
        default=5e7,
    )
    hard_filter_args.add_argument(
        "--min-bases-dp-over-20",
        type=float,
        help="Filtering threshold to use for the minimum number of bases with a DP over 20. Default is 4e7.",
        default=4e7,
    )
    hard_filter_args.add_argument(
        "--max-contamination",
        default=0.05,
        type=float,
        help=(
            "Filtering threshold to use for maximum contamination (this is a proportion not percent, "
            "e.g. 5% == 0.05, %5 != 5). Default is 0.05.",
        ),
    )
    hard_filter_args.add_argument(
        "--max-chimera",
        type=float,
        default=0.05,
        help=(
            "Filtering threshold to use for maximum chimera (this is a proportion not a percent, "
            "e.g. 5% == 0.05, %5 != 5). Default is 0.05."
        ),
    )
    hard_filter_args.add_argument(
        "--min-cov",
        help="Minimum chromosome 20 coverage for inclusion when computing hard-filters.",
        default=None,
        type=int,
    )
    hard_filter_args.add_argument(
        "--min-qc-mt-adj-callrate",
        help=(
            "Minimum sample callrate computed on only predetermined QC variants (predetermined using CCDG "
            "genomes/exomes, gnomAD v3.1 genomes, and UKBB exomes) after ADJ filtering."
        ),
        default=None,
        type=float,
    )
    parser.add_argument(
        "--slack-channel", help="Slack channel to post results and notifications to."
    )

    args = parser.parse_args()

    if args.slack_channel:
        with slack_notifications(slack_token, args.slack_channel):
            main(args)
    else:
        main(args)
