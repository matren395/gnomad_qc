import argparse
import logging

import hail as hl

from gnomad.sample_qc.filtering import compute_stratified_sample_qc
from gnomad.utils.annotations import bi_allelic_expr
from gnomad.utils.filtering import add_filters_expr
from gnomad.resources.grch38.reference_data import telomeres_and_centromeres

from gnomad_qc.v4.resources.basics import (
    calling_intervals,
    get_checkpoint_path,
    get_gnomad_v4_vds,
    get_logging_path,
    gnomad_v4_testset,
)
from gnomad_qc.v4.resources.meta import project_meta
from gnomad_qc.v4.resources.sample_qc import (
    fingerprinting,
    get_sample_qc,
    hard_filtered_samples,
    interval_coverage,
    sex,
)

logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger("hard_filters")
logger.setLevel(logging.INFO)


def compute_sample_qc(n_partitions: int = 1000, test: bool = False) -> hl.Table:
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
        tmp_ht_prefix=get_sample_qc().path[:-3],
        gt_col="LGT",
    )

    return sample_qc_ht.repartition(n_partitions)


def compute_hard_filters(
    coverage_mt: hl.Table,
    test: bool = False,
    include_sex_filter: bool = False,
    cov_threshold: int = 15,
    max_n_snp: float = 3.75e6,
    min_n_snp: float = 2.4e6,
    max_n_singleton: float = 1e5,
    max_r_het_hom_var: float = 3.3,
    max_contamination: float = 0.05,
    max_chimera: float = 0.05,
    min_n_over_gq_threshold: float = 27e9,
    min_gq_threshold: int = 20,
    min_n_over_dp_threshold: float = 2.7e9,
    min_dp_threshold: int = 10,
) -> hl.Table:
    """
    Apply hard filters to samples and return a Table with the filtered samples and the reason for filtering.

    This function expects a sex table generated by `impute_sex`.

    :param coverage_mt: MatrixTable containing the per interval per sample coverage statistics.
    :param test: Whether to use the gnomAD v4 test dataset. Default is to use the full dataset.
    :param include_sex_filter: If sex inference should be used in filtering.
    :param cov_threshold: Filtering threshold to use for chr20 min mean coverage.
    :param max_n_snp: Filtering threshold to use for the max number of SNPs.
    :param min_n_snp: Filtering threshold to use for the min number of SNPs.
    :param max_n_singleton: Filtering threshold to use for the max number of singletons.
    :param max_r_het_hom_var: Filtering threshold to use for the max ratio of heterozygotes to alternate homozygotes
    :param max_contamination: Filtering threshold to use for max contamination (this is a proportion not a
        percecnt, e.g. 5% == 0.05, %5 != 5).
    :param max_chimera: Filtering threshold to use for max chimera (this is a proportion not a percent,
        e.g. 5% == 0.05, %5 != 5).
    :param min_n_over_gq_threshold: Minimum number of bases with a GQ over the filtering threshold. Default is 2.7e9.
    :param min_gq_threshold: GQ threshold to use for filtering. Default is 20.
    :param min_n_over_dp_threshold: Minimum number of bases with a DP over the filtering threshold. Default is 2.7e9.
    :param min_dp_threshold: DP threshold to use for filtering. Default is 10.
    :return: Table of hard filtered samples.
    """
    ht = get_gnomad_v4_vds(remove_hard_filtered_samples=False, test=test).variant_data.cols()
    ht = ht.annotate_globals(
        hard_filter_cutoffs=hl.struct(
            min_cov=cov_threshold,
            max_n_snp=max_n_snp,
            min_n_snp=min_n_snp,
            max_n_singleton=max_n_singleton,
            max_r_het_hom_var=max_r_het_hom_var,
            max_contamination=max_contamination,
            max_chimera=max_chimera,
            min_n_over_gq_threshold=min_n_over_gq_threshold,
            gq_threshold=min_gq_threshold,
            min_n_over_dp_threshold=min_n_over_dp_threshold,
            dp_threshold=min_dp_threshold,
        ),
    )
    hard_filters = dict()

    # Flag samples failing fingerprinting
    fp_ht = fingerprinting.ht()
    hard_filters["failed_fingerprinting"] = hl.is_defined(fp_ht[ht.key])

    # Flag extreme raw bi-allelic sample QC outliers
    bi_allelic_qc_ht = get_sample_qc("bi_allelic").ht()
    # Convert tuples to lists so we can find the index of the passed threshold
    gq_bins = [
        hl.eval(bi_allelic_qc_ht.gq_bins[i])
        for i in range(len(bi_allelic_qc_ht.gq_bins))
    ]
    dp_bins = [
        hl.eval(bi_allelic_qc_ht.dp_bins[i])
        for i in range(len(bi_allelic_qc_ht.dp_bins))
    ]
    bi_allelic_qc_struct = bi_allelic_qc_ht[ht.key]
    # TODO: Do we still want all of these? We may want to make these cutoffs? We will need to look at distributions to
    #  determine this. There could also be other metrics we want to use instead, but will not know until we have
    #  distributions on the full set
    hard_filters["sample_qc_metrics"] = (
        (bi_allelic_qc_struct.n_snp > max_n_snp)
        | (bi_allelic_qc_struct.n_snp < min_n_snp)
        | (bi_allelic_qc_struct.n_singleton > max_n_singleton)
        | (bi_allelic_qc_struct.r_het_hom_var > max_r_het_hom_var)
        | (
            bi_allelic_qc_struct.bases_over_gq_threshold[
                gq_bins.index(min_gq_threshold)
            ]
            < min_n_over_gq_threshold
        )
        | (
            bi_allelic_qc_struct.bases_over_dp_threshold[
                dp_bins.index(min_dp_threshold)
            ]
            < min_n_over_dp_threshold
        )
    )

    # Flag samples that fail bam metric thresholds
    # TODO: Change this for the testing
    bam_metrics_struct = project_meta.ht()[ht.key].bam_metrics
    hard_filters["contamination"] = bam_metrics_struct.contam_rate > max_contamination
    hard_filters["chimera"] = bam_metrics_struct.chimeras_rate > max_chimera

    # Flag low-coverage samples
    coverage_mt = hl.filter_intervals(coverage_mt, [hl.parse_locus_interval('chr20')])
    coverage_ht = coverage_mt.select_cols(
        chr20_mean_dp=hl.agg.sum(coverage_mt.sum_dp) / hl.agg.sum(coverage_mt.interval_size)
    ).cols()

    hard_filters["low_coverage"] = (
        coverage_ht[ht.key].chr20_mean_dp < cov_threshold
    )

    if include_sex_filter:
        sex_struct = sex.ht()[ht.key]
        # Remove samples with ambiguous sex assignments
        hard_filters["ambiguous_sex"] = sex_struct.sex_karyotype == "ambiguous"
        # TODO: Confirm this is what we want to remove
        hard_filters[
            "sex_aneuploidy"
        ] = ~hl.set(  # pylint: disable=invalid-unary-operand-type
            {"ambiguous", "XX", "XY"}
        ).contains(
            sex_struct.sex_karyotype
        )

    ht = ht.annotate(hard_filters=add_filters_expr(filters=hard_filters))

    # Keep samples failing hard filters
    ht = ht.filter(hl.len(ht.hard_filters) > 0)
    return ht


def main(args):
    hl.init(log="/gnomad_hard_filters.log", default_reference="GRCh38")
    calling_interval_name = args.calling_interval_name
    calling_interval_padding = args.calling_interval_padding

    try:
        if args.sample_qc:
            compute_sample_qc(n_partitions=args.sample_qc_n_partitions, test=args.test).write(
                get_sample_qc().path, overwrite=args.overwrite
            )

        if args.compute_coverage:
            if args.test:
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
                if args.test
                else interval_coverage.path,
                overwrite=args.overwrite,
            )

        if args.compute_hard_filters:
            # TODO: Determine cutoffs by visual inspection of the metrics, and modify defaults to match
            if args.test:
                coverage_mt = hl.read_matrix_table(
                    get_checkpoint_path(
                        f"test_interval_coverage.{calling_interval_name}.pad{calling_interval_padding}",
                        mt=True,
                    )
                )
            else:
                interval_coverage.ht()

            compute_hard_filters(
                coverage_mt,
                args.include_sex_filter,
                args.min_cov,
                args.max_n_snp,
                args.min_n_snp,
                args.max_n_singleton,
                args.max_r_het_hom_var,
                args.max_contamination,
                args.max_chimera,
                args.min_n_over_gq_threshold,
                args.min_gq_threshold,
                args.min_n_over_dp_threshold,
                args.min_dp_threshold,
            ).write(hard_filtered_samples.path, overwrite=args.overwrite)

    finally:
        logger.info("Copying log to logging bucket...")
        hl.copy_log(get_logging_path("platform_pca"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--overwrite",
        help="Overwrite all data from this subset (default: False)",
        action="store_true",
    )
    parser.add_argument(
        "--test",
        help="Use the v4 test dataset instead of the full dataset.",
        action="store_true",
    )
    parser.add_argument(
        "--sample-qc", help="Compute Hail's VDS sample QC metrics", action="store_true"
    )
    parser.add_argument(
        "--sample-qc-n-partitions",
        help="Number of desired partitions for the sample QC output Table",
        default=1000,
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
    parser.add_argument(
        "--include-low-cov-filter",
        help="Whether to mean chr20 coverage in hard filtering.",
        action="store_true",
    )
    parser.add_argument_group()
    hard_filter_args = parser.add_argument_group(
        "Hard-filter cut-offs", "Arguments used for hard-filter cut-offs."
    )
    hard_filter_args.add_argument(
        "--min-cov",
        help="Minimum coverage for inclusion when computing hard-filters.",
        default=15,
        type=int,
    )
    hard_filter_args.add_argument(
        "--max-n-snp",
        type=float,
        default=3.75e6,
        help="Filtering threshold to use for the maximum number of SNPs. Default is 3.75e6.",
    )
    hard_filter_args.add_argument(
        "--min-n-snp",
        type=float,
        default=2.4e6,
        help="Filtering threshold to use for the minimum number of SNPs. Default is 2.4e6.",
    )
    hard_filter_args.add_argument(
        "--max-n-singleton",
        type=float,
        default=1e5,
        help="Filtering threshold to use for the max number of singletons. Default is 1e5.",
    )
    hard_filter_args.add_argument(
        "-max-r-het-hom-var",
        type=float,
        default=3.3,
        help="Filtering threshold to use for the max ratio of heterozygotes to alternate homozygotes. Default is 3.3.",
    )
    hard_filter_args.add_argument(
        "--max-contamination",
        default=0.05,
        type=float,
        help="Filtering threshold to use for max percent contamination (this is a proportion not percent, e.g. 5% == 0.05, %5 != 5). Default is 0.05.",
    )
    hard_filter_args.add_argument(
        "--max-chimera",
        type=float,
        default=0.05,
        help="Filtering threshold to use for max percent chimera (this is a proportion not a percent, e.g. 5% == 0.05, %5 != 5). Default is 0.05.",
    )
    hard_filter_args.add_argument(
        "--min-n-over-gq-threshold",
        type=float,
        help="Minimum number of bases with a GQ over the filtering threshold. Default is 2.7e9.",
        default=2.7e9,
    )
    hard_filter_args.add_argument(
        "--min-gq-threshold",
        type=int,
        help="Minimum GQ threshold to use for filtering.",
        choices=[0, 20, 60],
        default=20,
    )
    hard_filter_args.add_argument(
        "--min-n-over-dp-threshold",
        type=float,
        help="Minimum number of bases with a DP over the filtering threshold. Default is 2.7e9.",
        default=2.7e9,
    )
    hard_filter_args.add_argument(
        "--min-dp-threshold",
        type=int,
        help="Minimum DP threshold to use for filternig.",
        choices=[0, 1, 10, 20, 30],
        default=10,
    )

    main(parser.parse_args())
