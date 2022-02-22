import argparse
import logging
from typing import Tuple

import hail as hl

from gnomad.resources.grch38.reference_data import (
    telomeres_and_centromeres,
)
from gnomad.sample_qc.pipeline import annotate_sex
from gnomad.sample_qc.sex import get_ploidy_cutoffs, get_sex_expr

from gnomad_qc.v4.resources.basics import get_gnomad_v4_vds
from gnomad_qc.v4.resources.sample_qc import sex

logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger("sex_inference")
logger.setLevel(logging.INFO)


def compute_sex(aaf_threshold: float = 0.001, f_stat_cutoff: float = 0.5) -> hl.Table:
    """
    Impute sample sex based on X-chromosome heterozygosity and sex chromosome ploidy.

    Allele frequencies from v3.0 are used to prevent the need to densify the current version's sparse MT.

    :param aaf_threshold: Minimum alternate allele frequency to be used in f-stat calculations.
    :param f_stat_cutoff: f-stat to roughly divide 'XX' from 'XY' samples. Assumes XX samples are below cutoff and XY are above cutoff.
    :return: Table with inferred sex annotation
    :rtype: hl.Table
    """
    mt = get_gnomad_v4_vds(remove_hard_filtered_samples=True)

    # TODO: Determine what variants to use for f-stat calculation
    freq_ht = freq.versions["3"].ht()
    freq_ht = freq_ht.select(AF=freq_ht.freq[0].AF)
    freq_ht = freq_ht.filter(freq_ht.AF > aaf_threshold)

    sex_ht = annotate_sex(
        mt,
        excluded_intervals=telomeres_and_centromeres.ht(),
        aaf_threshold=aaf_threshold,
        f_stat_cutoff=f_stat_cutoff,
        sites_ht=freq_ht,
        aaf_expr="AF",
        gt_expr="LGT",
    )

    return sex_ht


def reannotate_sex(
        cov_threshold: int,
        x_ploidy_cutoffs: Tuple[float, Tuple[float, float], float],
        y_ploidy_cutoffs: Tuple[Tuple[float, float], float],
):
    """
    Rerun sex karyotyping annotations without re-computing sex imputation metrics.

    :param cov_threshold: Filtering threshold to use for chr20 coverage in `compute_hard_filters`
    :param x_ploidy_cutoffs: Tuple of X chromosome ploidy cutoffs: (upper cutoff for single X, (lower cutoff for double X, upper cutoff for double X), lower cutoff for triple X)
    :param y_ploidy_cutoffs: Tuple of Y chromosome ploidy cutoffs: ((lower cutoff for single Y, upper cutoff for single Y), lower cutoff for double Y)
    :return: Table with sex karyotyping annotations
    :rtype: hl.Table
    """
    # Copy HT to temp location to overwrite annotation
    sex_ht = sex.ht().checkpoint("gs://gnomad-tmp/sex_ht_checkpoint.ht", overwrite=True)
    hard_filter_ht = compute_hard_filters(cov_threshold, include_sex_filter=False)

    # Copy HT to temp location because it uses sex_ht for chr20 coverage
    hard_filter_ht = hard_filter_ht.checkpoint(
        "gs://gnomad-tmp/hardfilter_checkpoint.ht", overwrite=True
    )
    new_x_ploidy_cutoffs, new_y_ploidy_cutoffs = get_ploidy_cutoffs(
        sex_ht.filter(hl.is_missing(hard_filter_ht[sex_ht.key])), f_stat_cutoff=0.5
    )
    x_ploidy_cutoffs = hl.struct(
        upper_x=x_ploidy_cutoffs[0] if x_ploidy_cutoffs[0] else new_x_ploidy_cutoffs[0],
        lower_xx=x_ploidy_cutoffs[1][0]
        if x_ploidy_cutoffs[1][0]
        else new_x_ploidy_cutoffs[1][0],
        upper_xx=x_ploidy_cutoffs[1][1]
        if x_ploidy_cutoffs[1][1]
        else new_x_ploidy_cutoffs[1][1],
        lower_xxx=x_ploidy_cutoffs[2]
        if x_ploidy_cutoffs[2]
        else new_x_ploidy_cutoffs[2],
    )
    y_ploidy_cutoffs = hl.struct(
        lower_y=y_ploidy_cutoffs[0][0]
        if y_ploidy_cutoffs[0][0]
        else new_y_ploidy_cutoffs[0][0],
        upper_y=y_ploidy_cutoffs[0][1]
        if y_ploidy_cutoffs[0][1]
        else new_y_ploidy_cutoffs[0][1],
        lower_yy=y_ploidy_cutoffs[1]
        if y_ploidy_cutoffs[1]
        else new_y_ploidy_cutoffs[1],
    )
    sex_ht = sex_ht.annotate(
        **get_sex_expr(
            sex_ht.chrX_ploidy,
            sex_ht.chrY_ploidy,
            (
                x_ploidy_cutoffs["upper_x"],
                (x_ploidy_cutoffs["lower_xx"], x_ploidy_cutoffs["upper_xx"]),
                x_ploidy_cutoffs["lower_xxx"],
            ),
            (
                (y_ploidy_cutoffs["lower_y"], y_ploidy_cutoffs["upper_y"]),
                y_ploidy_cutoffs["lower_yy"],
            ),
        )
    )
    sex_ht = sex_ht.annotate_globals(
        x_ploidy_cutoffs=x_ploidy_cutoffs, y_ploidy_cutoffs=y_ploidy_cutoffs,
    )

    return sex_ht


def main(args):
    hl.init(log="/sex_inference.log", default_reference="GRCh38")

    if args.impute_sex:
        compute_sex().write(sex.path, overwrite=args.overwrite)
    elif args.reannotate_sex:
        reannotate_sex(
            args.min_cov,
            (args.upper_x, (args.lower_xx, args.upper_xx), args.lower_xxx),
            ((args.lower_y, args.upper_y), args.lower_yy),
        ).write(sex.path, overwrite=args.overwrite)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--overwrite",
        help="Overwrite all data from this subset (default: False)",
        action="store_true",
    )
    parser.add_argument(
        "--impute_sex",
        help="Runs sex imputation. Also runs sex karyotyping annotation.",
        action="store_true",
    )
    parser.add_argument(
        "--reannotate_sex",
        help="Runs the sex karyotyping annotations again, without re-computing sex imputation metrics.",
        action="store_true",
    )
    parser.add_argument("--upper_x", help="Upper cutoff for single X", type=float)
    parser.add_argument("--lower_xx", help="Lower cutoff for double X", type=float)
    parser.add_argument("--upper_xx", help="Upper cutoff for double X", type=float)
    parser.add_argument("--lower_xxx", help="Lower cutoff for triple X", type=float)
    parser.add_argument("--lower_y", help="Lower cutoff for single Y", type=float)
    parser.add_argument("--upper_y", help="Upper cutoff for single Y", type=float)
    parser.add_argument("--lower_yy", help="Lower cutoff for double Y", type=float)

    main(parser.parse_args())
