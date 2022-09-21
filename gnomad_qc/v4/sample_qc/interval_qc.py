import argparse
import logging
from typing import List, Tuple, Union

from gnomad.utils.slack import slack_notifications
import hail as hl

from gnomad_qc.slack_creds import slack_token
from gnomad_qc.v4.resources.basics import (
    calling_intervals,
    get_checkpoint_path,
    get_gnomad_v4_vds,
    get_logging_path,
)
from gnomad_qc.v4.resources.sample_qc import (
    hard_filtered_samples,
    interval_coverage,
    interval_qc,
    platform,
    sex,
    sex_chr_coverage,
)

logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger("interval_qc")
logger.setLevel(logging.INFO)


def generate_sex_chr_interval_coverage_mt(
    vds: hl.vds.VariantDataset,
    calling_intervals_ht: hl.Table,
) -> hl.MatrixTable:
    """
    Create a MatrixTable of interval-by-sample coverage on sex chromosomes with intervals split at PAR regions.

    :param vds: Input VariantDataset.
    :param calling_intervals_ht: Calling interval Table.
    :return: MatrixTable with interval coverage per sample on sex chromosomes.
    """
    contigs = ["chrX", "chrY"]
    calling_intervals_ht = calling_intervals_ht.filter(
        hl.literal(contigs).contains(calling_intervals_ht.interval.start.contig)
    )
    logger.info(
        "Filtering VariantDataset to the following contigs: %s...",
        ", ".join(contigs),
    )
    vds = hl.vds.filter_chromosomes(vds, keep=contigs)
    rg = vds.reference_data.locus.dtype.reference_genome

    par_boundaries = []
    for par_interval in rg.par:
        par_boundaries.append(par_interval.start)
        par_boundaries.append(par_interval.end)

    # Segment on PAR interval boundaries
    calling_intervals = hl.segment_intervals(calling_intervals_ht, par_boundaries)

    # Annotate intervals overlapping PAR
    calling_intervals = calling_intervals.annotate(
        overlap_par=hl.any(
            lambda x: x.overlaps(calling_intervals.interval), hl.literal(rg.par)
        )
    )

    kept_contig_filter = hl.array(contigs).map(
        lambda x: hl.parse_locus_interval(x, reference_genome=rg)
    )
    vds = hl.vds.VariantDataset(
        hl.filter_intervals(vds.reference_data, kept_contig_filter),
        hl.filter_intervals(vds.variant_data, kept_contig_filter),
    )
    mt = hl.vds.interval_coverage(vds, calling_intervals)
    mt = mt.annotate_rows(overlap_par=calling_intervals[mt.row_key].overlap_par)

    return mt


def filter_to_test(
    mt: hl.MatrixTable,
    sex_mt: hl.MatrixTable,
    num_partitions: int = 10,
) -> Tuple[hl.MatrixTable, hl.MatrixTable]:
    """
    Filter `mt` to `num_partitions` partitions on chr1 and `sex_mt` to `num_partitions` partitions on chrX and chrY.

    :param mt: Input MatrixTable to filter to specified number of partitions on chr1.
    :param sex_mt: Input MatrixTable to filter to specified number of partitions on chrX and all of chrY.
    :param num_partitions: Number of partitions to grab from mt.
    :return: Input MatrixTables filtered to `num_partitions` on chr1, chrX, and all of chrY.
    """
    logger.info(
        "Filtering to columns in both the coverage MT and the sex coverage MT for testing...",
    )
    mt = mt.anti_join_cols(sex_mt.cols())
    sex_mt = sex_mt.anti_join_cols(mt.cols())

    logger.info(
        "Filtering to %d partitions on chr1, chrX, and all of chrY for testing...",
        num_partitions,
    )
    mt = mt._filter_partitions(range(num_partitions))
    sex_mt_chrx = sex_mt._filter_partitions(range(num_partitions))
    sex_mt_chry = sex_mt.filter_rows(
        (sex_mt.interval.start.contig == "chrY")
    ).repartition(100)
    sex_mt_chry = sex_mt_chry._filter_partitions(range(num_partitions))

    return mt, sex_mt_chrx.union_rows(sex_mt_chry)


def compute_interval_qc(
    mt: hl.MatrixTable,
    platform_ht: hl.Table,
    mean_dp_thresholds: List[int] = [5, 10, 15, 20, 25],
    split_by_sex: bool = False,
) -> hl.Table:
    """


    :param mt: Input interval coverage MatrixTable
    :param platform_ht: Input platform assignment Table.
    :param mean_dp_thresholds: List of mean DP thresholds to use for computing the fraction of samples with mean
        interval DP >= the threshold.
    :param bool split_by_sex: Whether the interval QC should be stratified by sex. If True, mt must be annotated with sex_karyotype.
    :return: Table with interval QC annotations
    """

    def _get_agg_expr(expr, agg_func=hl.agg.mean, group_by=None):
        """

        :param expr:
        :param agg_func:
        :return:
        """
        if group_by is not None:
            agg_func = hl.agg.group_by(group_by, agg_func(expr))
        else:
            agg_func = agg_func(expr)
        agg_expr = {"all": agg_func}
        if split_by_sex:
            agg_expr.update(
                {
                    "XX": hl.agg.filter(mt.sex_karyotype == "XX", agg_func),
                    "XY": hl.agg.filter(mt.sex_karyotype == "XY", agg_func),
                }
            )

        return agg_expr

    mt = mt.annotate_cols(platform=platform_ht[mt.col_key].qc_platform)
    agg_groups = [("", None), ("platform_", mt.platform)]
    mt = mt.select_rows(
        **{
            f"{prefix}interval_mean_dp": _get_agg_expr(mt.mean_dp, group_by=group_by)
            for prefix, group_by in agg_groups
        },
        **{
            f"{prefix}prop_samples_by_dp": hl.struct(
                **{
                    f"over_{dp}x": _get_agg_expr(
                        mt.mean_dp >= dp, agg_func=hl.agg.fraction, group_by=group_by
                    )
                    for dp in mean_dp_thresholds
                }
            )
            for prefix, group_by in agg_groups
        },
        **{
            f"{prefix}mean_fraction_over_dp_0": _get_agg_expr(
                mt.fraction_over_dp_threshold[1], group_by=group_by
            )
            for prefix, group_by in agg_groups
        },
    )

    mt = mt.annotate_globals(
        mean_dp_thresholds=mean_dp_thresholds,
        platform_n_samples=mt.aggregate_cols(
            hl.agg.group_by(mt.platform, hl.agg.count())
        ),
    )

    return mt.rows()


def get_interval_qc_pass(
    interval_qc_ht: hl.Table,
    per_platform: bool = False,
    all_platforms: bool = False,
    min_platform_size: int = 100,
    by_mean_fraction_over_dp_0: bool = True,
    by_prop_samples_over_cov: bool = False,
    mean_fraction_over_dp_0: float = 0.99,
    autosome_par_xx_cov: int = 20,
    xy_nonpar_cov: int = 10,
    prop_samples: float = 0.85,
) -> hl.Table:
    """
    Add `interval_qc_pass` annotation to indicate whether the site falls within a high coverage interval.

    :param interval_qc_ht: Input interval QC Table.
    :param per_platform: Whether filter to per platform high coverage intervals for the sex ploidy imputation.
    :param all_platforms: Whether to filter to high coverage intervals for the sex ploidy imputation. Use only intervals that are considered high coverage across all platforms.
    :param by_mean_fraction_over_dp_0: Whether to use the mean fraction of bases over DP 0 to determine high coverage intervals.
    :param by_prop_samples_over_cov: Whether to determine high coverage intervals using the proportion of samples with a mean interval coverage over a specified coverage for chrX (--x-cov), chrY (--y-cov), and the normalization contig (--norm-cov).
    :param mean_fraction_over_dp_0: Mean fraction of bases over DP used to define high coverage intervals. Default is 0.99.
    :param autosome_par_xx_cov: Mean coverage level used to define high coverage intervals on the the autosomes/sex chr par/female X. Default is 20.
    :param xy_nonpar_cov: Mean coverage level used to define high coverage intervals on male X and Y. This field must be in the sex interval coverage MT. Default is 10.
    :param prop_samples: Proportion of samples with mean coverage greater than `autosome_cov`/`sex_cov` over the interval to determine high coverage intervals. Default is 0.85.
    :return: MatrixTable or Table with samples removed
    """
    if (by_mean_fraction_over_dp_0 and by_prop_samples_over_cov) or (
        not by_mean_fraction_over_dp_0 and not by_prop_samples_over_cov
    ):
        raise ValueError(
            "One and only one of 'high_cov_by_mean_fraction_over_dp_0' and 'high_cov_by_prop_samples_over_cov' must be "
            "True!"
        )
    if per_platform and all_platforms:
        raise ValueError("Only one of 'per_platform' and 'all_platforms' can be True!")

    interval_qc_ht = interval_qc_ht.annotate_globals(
        per_platform=per_platform,
        all_platforms=all_platforms,
    )
    interval_start = interval_qc_ht.interval.start
    autosome_or_par = interval_start.in_autosome_or_par() | interval_qc_ht.overlap_par
    x_non_par = interval_start.in_x_nonpar() & ~interval_qc_ht.overlap_par
    y_non_par = interval_start.in_y_nonpar() & ~interval_qc_ht.overlap_par

    if per_platform or all_platforms:
        platform_n_samples = (
            interval_qc_ht.index_globals().platform_n_samples.collect()[0]
        )
        ann_prefix = "platform_"
    else:
        ann_prefix = ""

    if by_mean_fraction_over_dp_0:
        add_globals = hl.struct(mean_fraction_over_dp_0=mean_fraction_over_dp_0)
        qc_expr = interval_qc_ht[f"{ann_prefix}mean_fraction_over_dp_0"]
        qc_autosome_par_expr = qc_expr["all"]
        qc_xx_expr = qc_expr.get("XX", None)
        qc_xy_expr = qc_expr.get("XY", None)
        cutoff = mean_fraction_over_dp_0
    if by_prop_samples_over_cov:
        add_globals = hl.struct(
            autosome_par_xx_cov=autosome_par_xx_cov,
            xy_nonpar_cov=xy_nonpar_cov,
            prop_samples=prop_samples,
        )
        qc_expr = interval_qc_ht[f"{ann_prefix}prop_samples_by_dp"]
        qc_autosome_par_expr = qc_expr[f"over_{autosome_par_xx_cov}x"]["all"]
        qc_xx_expr = qc_expr[f"over_{autosome_par_xx_cov}x"].get("XX", None)
        qc_xy_expr = qc_expr[f"over_{xy_nonpar_cov}x"].get("XY", None)
        cutoff = prop_samples

    def _get_pass_expr(qc_autosome_par_expr, qc_xx_expr, qc_xy_expr):
        return (
            (autosome_or_par & (qc_autosome_par_expr > cutoff))
            | (x_non_par & (qc_xx_expr > cutoff) & (qc_xy_expr > cutoff))
            | (y_non_par & (qc_xy_expr > cutoff))
        )

    if per_platform or all_platforms:
        interval_qc_ht = interval_qc_ht.select(
            pass_interval_qc=hl.struct(
                **{
                    platform: _get_pass_expr(
                        qc_autosome_par_expr[platform],
                        qc_xx_expr[platform],
                        qc_xy_expr[platform],
                    )
                    for platform in platform_n_samples
                }
            )
        )
        if all_platforms:
            add_globals = add_globals.annotate(min_platform_size=min_platform_size)
            platforms = [
                platform
                for platform, n_samples in platform_n_samples.items()
                if (n_samples >= min_platform_size) & (platform != "platform_-1")
            ]
            interval_qc_ht = interval_qc_ht.select(
                pass_interval_qc=hl.all(
                    [
                        interval_qc_ht.pass_interval_qc[platform]
                        for platform in platforms
                    ]
                )
            )
    else:
        interval_qc_ht = interval_qc_ht.select(
            pass_interval_qc=_get_pass_expr(
                qc_autosome_par_expr,
                qc_xx_expr,
                qc_xy_expr,
            )
        )
    interval_qc_ht = interval_qc_ht.annotate_globals(**add_globals)

    return interval_qc_ht


def annotate_interval_qc_filter(
    t: Union[hl.MatrixTable, hl.Table],
    interval_qc_ht,
    **kwargs,
) -> Union[hl.MatrixTable, hl.Table]:
    # interval_qc_ht = interval_qc.ht()
    interval_qc_ht = get_interval_qc_pass(interval_qc_ht, **kwargs)

    if isinstance(t, hl.MatrixTable):
        t = t.annotate_rows(interval_qc_pass=interval_qc_ht[t.locus].pass_interval_qc)
    else:
        t = t.annotate(interval_qc_pass=interval_qc_ht[t.locus].pass_interval_qc)

    return t


def main(args):
    hl.init(
        log="/interval_qc.log",
        default_reference="GRCh38",
        tmp_dir="gs://gnomad-tmp-4day",
    )
    test = args.test
    calling_interval_name = args.calling_interval_name
    calling_interval_padding = args.calling_interval_padding
    overwrite = args.overwrite

    try:
        if args.sex_chr_interval_coverage:
            vds = get_gnomad_v4_vds(
                remove_hard_filtered_samples=False,
                remove_hard_filtered_samples_no_sex=True,
                test=test,
            )
            calling_intervals_ht = calling_intervals(
                calling_interval_name, calling_interval_padding
            ).ht()
            sex_coverage_mt = generate_sex_chr_interval_coverage_mt(
                vds,
                calling_intervals_ht,
            )
            sex_coverage_mt = sex_coverage_mt.annotate_globals(
                calling_interval_name=calling_interval_name,
                calling_interval_padding=calling_interval_padding,
            )
            sex_coverage_mt.write(
                get_checkpoint_path("test_sex_imputation_cov", mt=True)
                if test
                else sex_chr_coverage.path,
                overwrite=args.overwrite,
            )

        if args.generate_interval_qc_ht:
            platform_ht = platform.ht()
            coverage_mt = interval_coverage.mt()
            sex_coverage_mt = sex_imputation_coverage.mt()
            if args.test:
                coverage_mt, sex_coverage_mt = filter_to_test(
                    coverage_mt, sex_coverage_mt
                )
                coverage_mt = coverage_mt.checkpoint(
                    get_checkpoint_path("interval_qc_coverage", mt=True),
                    _read_if_exists=True,
                )
                sex_coverage_mt = sex_coverage_mt.checkpoint(
                    get_checkpoint_path("interval_qc_sex_coverage", mt=True),
                    _read_if_exists=True,
                )

            logger.info("Removing hard-filtered samples from the coverage MTs...")
            coverage_mt = coverage_mt.filter_cols(
                hl.is_missing(hard_filtered_samples.ht()[coverage_mt.col_key])
            )
            sex_coverage_mt = sex_coverage_mt.filter_cols(
                hl.is_missing(hard_filtered_samples.ht()[sex_coverage_mt.col_key])
            )

            coverage_mt = coverage_mt.filter_rows(
                coverage_mt.interval.start.in_autosome()
            )
            ht = compute_interval_qc(
                coverage_mt,
                platform_ht=platform_ht,
                mean_dp_thresholds=coverage_mt.mean_dp_thresholds,
            )

            logger.info("Filtering to XX and XY samples...")
            sex_ht = sex.ht().select("sex_karyotype")
            sex_coverage_mt = sex_coverage_mt.annotate_cols(
                **sex_ht[sex_coverage_mt.col_key]
            )
            sex_coverage_mt = sex_coverage_mt.filter_cols(
                (sex_coverage_mt.sex_karyotype == "XX")
                | (sex_coverage_mt.sex_karyotype == "XY")
            )

            ht = ht.union(
                compute_interval_qc(
                    sex_coverage_mt,
                    platform_ht=platform_ht,
                    mean_dp_thresholds=sex_coverage_mt.mean_dp_thresholds,
                    split_by_sex=True,
                )
            )
            ht.write(
                get_checkpoint_path("interval_qc") if args.test else interval_qc.path,
                overwrite=args.overwrite,
            )
        if args.interval_qc_pass_ht:
            ht = (
                hl.read_table(get_checkpoint_path("interval_qc"))
                if args.test
                else interval_qc.ht()
            )
            ht = get_interval_qc_pass(
                ht,
                per_platform=False,
                all_platforms=False,
                by_mean_fraction_over_dp_0=True,
                by_prop_samples_over_cov=False,
            )
            # ht.write(
            #    get_checkpoint_path("interval_qc") if args.test else interval_qc.path,
            #    overwrite=args.overwrite,
            # )
    finally:
        logger.info("Copying log to logging bucket...")
        hl.copy_log(get_logging_path("interval_qc"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--overwrite",
        help="Overwrite output files.",
        action="store_true",
    )
    parser.add_argument(
        "--test",
        help="Test using only 2 partitions on chr20, chrX, and chrY.",
        action="store_true",
    )
    parser.add_argument(
        "--slack-channel", help="Slack channel to post results and notifications to."
    )

    sex_coverage_args = parser.add_argument_group(
        "Sex imputation interval coverage",
        "Arguments used for computing interval coverage for sex imputation.",
    )
    sex_coverage_args.add_argument(
        "--sex-imputation-interval-coverage",
        help=(
            "Create a MatrixTable of interval-by-sample coverage on a specified list of contigs with PAR regions "
            "excluded."
        ),
        action="store_true",
    )
    sex_coverage_args.add_argument(
        "--calling-interval-name",
        help=(
            "Name of calling intervals to use for interval coverage. One of: 'ukb', 'broad', or 'intersection'. Only "
            "used if '--test' is set."
        ),
        type=str,
        choices=["ukb", "broad", "intersection"],
        default="intersection",
    )
    sex_coverage_args.add_argument(
        "--calling-interval-padding",
        help=(
            "Number of base pair padding to use on the calling intervals. One of 0 or 50 bp. Only used if '--test' is "
            "set."
        ),
        type=int,
        choices=[0, 50],
        default=50,
    )

    args = parser.parse_args()
    main(args)

    if args.slack_channel:
        with slack_notifications(slack_token, args.slack_channel):
            main(args)
    else:
        main(args)
