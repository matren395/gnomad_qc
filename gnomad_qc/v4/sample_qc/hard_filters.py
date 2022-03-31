import argparse
import logging

import hail as hl

from gnomad.sample_qc.filtering import compute_stratified_sample_qc
from gnomad.utils.annotations import bi_allelic_expr
from gnomad.utils.filtering import add_filters_expr
from gnomad.resources.grch38.reference_data import telomeres_and_centromeres

from gnomad_qc.v4.resources.basics import get_gnomad_v4_vds
from gnomad_qc.v4.resources.meta import project_meta
from gnomad_qc.v4.resources.sample_qc import (
    fingerprinting,
    get_sample_qc,
    hard_filtered_samples,
    sex,
)

logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger("hard_filters")
logger.setLevel(logging.INFO)


def compute_sample_qc() -> hl.Table:
    """
    Perform sample QC on the raw split matrix table using `compute_stratified_sample_qc`.
    :return: Table containing sample QC metrics
    :rtype: hl.Table
    """
    logger.info("Computing sample QC")
    vds = get_gnomad_v4_vds(split=True, remove_hard_filtered_samples=False)
    vds = hl.vds.filter_chromosomes(vds, keep_autosomes=True)

    # Remove centromeres and telomeres incase they were included
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

    return sample_qc_ht.repartition(100)


def compute_hard_filters(
    include_sex_filter=False,
    include_sex_cov_filter=False,
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
    :param include_sex_filter: If sex inference should be used in filtering.
    :param include_sex_cov_filter: If the sex ht's chr20 coverage should be used for a coverage filter.
    :param cov_threshold: Filtering threshold to use for chr20 coverage
    :param max_n_snp: Filtering threshold to use for the max number of SNPs
    :param min_n_snp: Filtering threshold to use for the min number of SNPs
    :param max_n_singleton: Filtering threshold to use for the max number of singletons
    :param max_r_het_hom_var: Filtering threshold to use for the max ratio of heterozygotes to alternate homozygotes
    :param max_contamination: Filtering threshold to use for max contamination (this is a proportion not a
        percecnt, e.g. 5% == 0.05, %5 != 5)
    :param max_chimera: Filtering threshold to use for max chimera (this is a proportion not a percent,
        e.g. 5% == 0.05, %5 != 5)
    :param min_n_over_gq_threshold: Minimum number of bases with a GQ over the filtering threshold. Default is 2.7e9.
    :param min_gq_threshold: GQ threshold to use for filtering. Default is 20.
    :param min_n_over_dp_threshold: Minimum number of bases with a DP over the filtering threshold. Default is 2.7e9.
    :param min_dp_threshold: DP threshold to use for filtering. Default is 10.
    :return: Table of hard filtered samples
    """
    ht = get_gnomad_v4_vds(remove_hard_filtered_samples=False).variant_data.cols()
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
    #  TODO: Determine cutoffs by visual inspection of the metrics
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
    hard_filters["bad_qc_metrics"] = (  # TODO: Do we want more detail in the flag?
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
    bam_metrics_struct = project_meta.ht()[ht.key].bam_metrics
    hard_filters["contamination"] = bam_metrics_struct.contam_rate > max_contamination
    hard_filters["chimera"] = bam_metrics_struct.chimeras_rate > max_chimera

    if include_sex_cov_filter:  # Flag low-coverage samples
        sex_ht = sex.ht()
        hard_filters["low_coverage"] = (
            sex_ht[ht.key].chr20_mean_dp < cov_threshold
        )  #  TODO: Confirm still using sex ht mean chr20 dp, not another metric
        # If keeping this, it needs to move into the include_sex_filter conditional
        # because we need the other hard filters prior to the sex_ht generation.

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

    ht = ht.annotate(hard_filters=add_filters_expr(filters=hard_filters))

    # Keep samples failing hard filters
    ht = ht.filter(hl.len(ht.hard_filters) > 0)
    return ht


def main(args):
    hl.init(log="/gnomad_hard_filters.log", default_reference="GRCh38")

    if args.sample_qc:
        compute_sample_qc().write(get_sample_qc().path, overwrite=args.overwrite)

    if args.compute_hard_filters:
        compute_hard_filters(
            args.include_sex_filter,
            args.include_sex_cov_filter,
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--overwrite",
        help="Overwrite all data from this subset (default: False)",
        action="store_true",
    )
    parser.add_argument(
        "--sample-qc", help="Compute Hail's VDS sample QC metrics", action="store_true"
    )
    parser.add_argument(
        "--compute-hard-filters",
        help="Computes samples to be hard-filtered",
        action="store_true",
    )
    parser.add_argument(
        "--include-sex-filter",
        help="If sex filters should be included in hard filtering.",
        action="store_true",
    )
    parser.add_argument(
        "--include-sex-cov-filter",
        help="Whether to use the sex ht's chr20 coverage for a coverage filter",
        action="store_true",
    )
    parser.add_argument_group()
    hard_filter_args = parser.add_argument_group(
        "Hard filter cut-offs", "Arguments used for hard filter cut-offs"
    )
    hard_filter_args.add_argument(
        "--min-cov",
        help="Minimum coverage for inclusion when computing hard-filters",
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
        help="Filtering threshold to use for max percent contamination (this is a proportion not percent, e.g. 5% == 0.05, %5 != 5). Default is 0.05",
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
