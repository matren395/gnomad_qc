import argparse
import logging

import hail as hl

from gnomad.sample_qc.filtering import compute_stratified_sample_qc
from gnomad.utils.annotations import bi_allelic_expr
from gnomad.utils.filtering import (
    add_filters_expr,
    filter_low_conf_regions,
    filter_to_autosomes,
)

from gnomad_qc.v4.resources.basics import get_gnomad_v4_vds
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
    mt = filter_to_autosomes(
        get_gnomad_v4_vds(
            split=True,
            remove_hard_filtered_samples=False,
        ).variant_data
    )
    mt = mt.select_entries("GT")


    # Remove centromeres and telomeres incase they were included
    mt = filter_low_conf_regions(
        mt,
        filter_lcr=False,
        filter_decoy=False,
        filter_segdup=False,
        filter_telomeres_and_centromeres=True,
    )

    sample_qc_ht = compute_stratified_sample_qc(
        mt,
        strata={
            "bi_allelic": bi_allelic_expr(mt),
            "multi_allelic": ~bi_allelic_expr(mt),
        },
        tmp_ht_prefix=get_sample_qc().path[:-3],
    )

    # Remove annotations that cannot be computed from the sparse format
    sample_qc_ht = sample_qc_ht.annotate(
        **{
            x: sample_qc_ht[x].drop(
                "n_called", "n_not_called", "n_filtered", "call_rate"
            )
            for x in sample_qc_ht.row_value
        }
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

    # Remove samples failing fingerprinting
    # TODO: Add these into hard filtering metadata when incorporating internal smaples Picard metrics
    hard_filters["failed_fingerprinting"] = hl.array(
        ["09C90823", "10C103592", "S5530"]
    ).contains(ht.s)

    # Remove low-coverage samples
    # chrom 20 coverage is computed to infer sex and used here
    cov_ht = sex.ht()
    hard_filters["low_coverage"] = cov_ht[ht.key].chr20_mean_dp < cov_threshold

    # Remove extreme raw bi-allelic sample QC outliers
    # These were determined by visual inspection of the metrics
    bi_allelic_qc_struct = get_sample_qc("bi_allelic").ht()[ht.key]
    hard_filters["bad_qc_metrics"] = (
            (bi_allelic_qc_struct.sample_qc.n_snp > max_n_snp)
            | (bi_allelic_qc_struct.sample_qc.n_snp < min_n_snp)
            | (bi_allelic_qc_struct.sample_qc.n_singleton > max_n_singleton)
            | (bi_allelic_qc_struct.sample_qc.r_het_hom_var > max_r_het_hom_var)
    )

    # Remove samples that fail picard metric thresholds
    picard_ht = picard_metrics.ht()[ht.key]
    hard_filters["contamination"] = (
            picard_ht.bam_metrics.freemix > max_pct_contamination
    )
    hard_filters["chimera"] = picard_ht.bam_metrics.pct_chimeras > max_pct_chimera
    hard_filters["insert_size"] = (
            picard_ht.bam_metrics.median_insert_size < min_median_insert_size
    )

    ht = ht.annotate(hard_filters=add_filters_expr(filters=hard_filters))

    ht = ht.filter(hl.len(ht.hard_filters) > 0)
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
    hl.init(log="/hail.log", default_reference="GRCh38")

    if args.sample_qc:
        compute_sample_qc().write(get_sample_qc().path, overwrite=args.overwrite)

    if args.compute_hard_filters:
        compute_hard_filters(args.min_cov).write(
            hard_filtered_samples.path, overwrite=args.overwrite
        )


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
    parser.add_argument(
        "--min_cov",
        help="Minimum coverage for inclusion when computing hard-filters",
        default=15,
        type=int,
    )


    main(parser.parse_args())
