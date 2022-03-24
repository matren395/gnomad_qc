import argparse
import logging

import hail as hl

from gnomad.sample_qc.filtering import compute_stratified_sample_qc
from gnomad.utils.annotations import bi_allelic_expr
from gnomad.utils.filtering import add_filters_expr
from gnomad.resources.grch38.reference_data import telomeres_and_centromeres

from gnomad_qc.v4.resources.basics import get_gnomad_v4_vds
from gnomad_qc.v4.resources import meta_tsv_path
from gnomad_qc.v4.resources.sample_qc import (
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
    vds = hl.vds.filter_intervals(vds, intervals=telomeres_and_centromeres, keep=False)

    sample_qc_ht = compute_stratified_sample_qc(
        vds,
        strata={
            "bi_allelic": bi_allelic_expr(vds.variant_data),
            "multi_allelic": ~bi_allelic_expr(vds.variant_data),
        },
        tmp_ht_prefix=get_sample_qc().path[:-3],
        gt_col="LGT",  # TODO: Determine if hl.vds.lgt_to_gt should be run first?
    )

    return sample_qc_ht.repartition(100)


def compute_hard_filters(
    cov_threshold: int = 15,
    max_n_snp: float = 3.75e6,
    min_n_snp: float = 2.4e6,
    max_n_singleton: float = 1e5,
    max_r_het_hom_var: float = 3.3,
    max_pct_contamination: float = 5.00,
    max_pct_chimera: float = 5.00,
) -> hl.Table:
    """
    Apply hard filters to samples and return Table with samples and the reason for filtering.

    This function expects a sex table generated by `impute_sex`.

    :param cov_threshold: Filtering threshold to use for chr20 coverage
    :param max_n_snp: Filtering threshold to use for the max number of SNPs
    :param min_n_snp: Filtering threshold to use for the min number of SNPs
    :param max_n_singleton: Filtering threshold to use for the max number of singletons
    :param max_r_het_hom_var: Filtering threshold to use for the max ratio of heterozygotes to alternate homozygotes
    :param max_pct_contamination: Filtering threshold to use for max percent contamination (this is a percent not a
        proportion, e.g. 5% == 5.00, %5 != 0.05)
    :param max_pct_chimera: Filtering threshold to use for max percent chimera (this is a percent not a proportion,
        e.g. 5% == 5.00, %5 != 0.05)
    :return: Table of hard filtered samples
    :rtype: hl.Table
    """
    ht = get_gnomad_v4_vds(remove_hard_filtered_samples=False).variant_data.cols()
    hard_filters = dict()

    # Flag samples failing fingerprinting
    # TODO: Need to update this once Julia runs the fingerprint check, assuming there will be a results file
    fp_ht = fingerprinting.ht()
    hard_filters["failed_fingerprinting"] = hl.is_defined(fp_ht[ht.key])

    # Flag low-coverage samples
    # chrom 20 coverage is computed to infer sex and used here
    sex_ht = sex.ht()
    hard_filters["low_coverage"] = (
        sex_ht[ht.key].chr20_mean_dp < cov_threshold
    )  # TODO: Confirm still using sex ht mean chr20 dp, not a picard metric or the interval coverage mt

    #  TODO: Confirm what we will filter on from sex ht, e.g. "ambiguous_sex", "sex_aneuploidy"
    #        CCDG used female_f_stat and male_f-stat, if we use this we would want to change
    #        the language to XX_f_stat and XY_f_stat

    hard_filters["ambiguous_sex"] = sex_ht.ambiguous_sex
    hard_filters["sex_aneuploidy"] = sex_ht.sex_aneuploidy

    # Flag extreme raw bi-allelic sample QC outliers
    # These were determined by visual inspection of the metrics
    bi_allelic_qc_struct = (
        get_sample_qc("bi_allelic").ht()[ht.key].bi_allelic_sample_qc
    )  # TODO: Update to if create get_sample_qc method not created
    hard_filters["bad_qc_metrics"] = (
        (bi_allelic_qc_struct.n_snp > max_n_snp)
        | (bi_allelic_qc_struct.n_snp < min_n_snp)
        | (bi_allelic_qc_struct.n_singleton > max_n_singleton)
        | (bi_allelic_qc_struct.r_het_hom_var > max_r_het_hom_var)
        | ()
    )

    # Flag samples that fail picard metric thresholds
    picard_ht = hl.import_table(meta_tsv_path(), force=True, impute=True)[
        ht.key
    ]  # TODO: update to resource once created
    hard_filters["contamination"] = (
        picard_ht.contam_rate > max_pct_contamination
    )  # TODO: Confirm this is a percent not proportion
    hard_filters["chimera"] = (
        picard_ht.chimeras_rate > max_pct_chimera
    )  # TODO: Confirm comparison is accurate, this is a percent not proportion

    ht = ht.annotate(hard_filters=add_filters_expr(filters=hard_filters))

    # Remove samples failing hard filters
    ht = ht.filter(hl.len(ht.hard_filters) > 0, keep=False)
    ht = ht.annotate_globals(
        hard_filter_cutoffs=hl.struct(
            min_cov=cov_threshold,
            max_n_snp=max_n_snp,
            min_n_snp=min_n_snp,
            max_n_singleton=max_n_singleton,
            max_r_het_hom_var=max_r_het_hom_var,
            max_pct_contamination=max_pct_contamination,
            max_pct_chimera=max_pct_chimera,
        ),
    )

    return ht


def main(args):
    hl.init(log="/gnomad_hard_filters.log", default_reference="GRCh38")

    if args.sample_qc:
        compute_sample_qc().write(get_sample_qc().path, overwrite=args.overwrite)

    if args.compute_hard_filters:  #  TODO: Will need to add args is we filter on f-stat
        compute_hard_filters(
            args.min_cov,
            args.max_n_snp,
            args.min_n_snp,
            args.max_n_singleton,
            args.max_r_het_hom_var,
            args.max_contamination,
            args.max_chimera,
        ).write(hard_filtered_samples.path, overwrite=args.overwrite)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--overwrite",
        help="Overwrite all data from this subset (default: False)",
        action="store_true",
    )
    parser.add_argument(
        "--sample_qc", help="Compute Hail's VDS sample QC metrics", action="store_true"
    )
    parser.add_argument(
        "--compute_hard_filters",
        help="Computes samples to be hard-filtered",
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
        type=int,
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
        default=5.0,
        type=float,
        help="Filtering threshold to use for max percent contamination (this is a percent not a proportion, e.g. 5% == 5.00, %5 != 0.05). Default is 5.0",
    )
    hard_filter_args.add_argument(
        "--max-chimera",
        type=float,
        default=5.00,
        help="Filtering threshold to use for max percent chimera (this is a percent not a proportion, e.g. 5% == 5.00, %5 != 0.05). Default is 5.0.",
    )

    main(parser.parse_args())
