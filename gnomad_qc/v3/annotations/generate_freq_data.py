import argparse
import logging

import hail as hl

from gnomad.resources.grch38.gnomad import (
    COHORTS_WITH_POP_STORED_AS_SUBPOP,
    DOWNSAMPLINGS,
    POPS_STORED_AS_SUBPOPS,
    POPS_TO_REMOVE_FOR_POPMAX,
    SUBSETS,
)
from gnomad.sample_qc.sex import adjusted_sex_ploidy_expr
from gnomad.utils.annotations import (
    age_hists_expr,
    annotate_freq,
    bi_allelic_site_inbreeding_expr,
    faf_expr,
    get_adj_expr,
    pop_max_expr,
    qual_hist_expr,
)
from gnomad.utils.release import (
    make_faf_index_dict,
    make_freq_index_dict,
)
from gnomad.utils.annotations import set_female_y_metrics_to_na_expr
from gnomad.utils.slack import slack_notifications

from gnomad_qc.slack_creds import slack_token
from gnomad_qc.v3.resources.annotations import get_freq
from gnomad_qc.v3.resources.basics import (
    get_checkpoint_path,
    get_gnomad_v3_mt,
    qc_temp_prefix,
)
from gnomad_qc.v3.utils import hom_alt_depletion_fix

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s: %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
)
logger = logging.getLogger("gnomAD_frequency_data")
logger.setLevel(logging.INFO)


def main(args):
    subsets = args.subsets
    hl.init(
        log=f"/generate_frequency_data{'.' + '_'.join(subsets) if subsets else ''}.log",
        default_reference="GRCh38",
    )

    if subsets:
        invalid_subsets = []
        n_subsets_use_subpops = 0
        for s in subsets:
            if s not in SUBSETS:
                invalid_subsets.append(s)
            if s in COHORTS_WITH_POP_STORED_AS_SUBPOP:
                n_subsets_use_subpops += 1

        if invalid_subsets:
            raise ValueError(
                f"{', '.join(invalid_subsets)} subset(s) are not one of the following official subsets: {SUBSETS}"
            )
        if n_subsets_use_subpops & (n_subsets_use_subpops != len(subsets)):
            raise ValueError(
                f"Cannot combine cohorts that use subpops in frequency calculations {COHORTS_WITH_POP_STORED_AS_SUBPOP} "
                f"with cohorts that use pops in frequency calculations {[s for s in SUBSETS if s not in COHORTS_WITH_POP_STORED_AS_SUBPOP]}."
            )

    try:
        logger.info(
            "Reading dense MT containing only sites that may require frequency recalculation due to het non ref site error. "
            "This dense MT only contains release samples and has already been split"
        )
        mt = hl.read_matrix_table(
            "gs://gnomad-tmp/release_3.1.2/het_nonref_fix_sites_3.1.2_test.mt"
            if args.test
            else "gs://gnomad-tmp/release_3.1.2/het_nonref_fix_sites.mt"
        )

        if args.test:
            logger.info("Filtering to two partitions on chr20")
            mt = hl.filter_intervals(mt, [hl.parse_locus_interval("chr20:1-1000000")])
            mt = mt._filter_partitions(range(2))

        if subsets:
            mt = mt.filter_cols(hl.any([mt.meta.subsets[s] for s in subsets]))
            logger.info(
                f"Running frequency generation pipeline on {mt.count_cols()} samples in {', '.join(subsets)} subset(s)..."
            )
        else:
            logger.info(
                f"Running frequency generation pipeline on {mt.count_cols()} samples..."
            )

        logger.info("Computing adj and sex adjusted genotypes...")
        mt = mt.annotate_entries(
            GT=adjusted_sex_ploidy_expr(
                mt.locus, mt.GT, mt.meta.sex_imputation.sex_karyotype
            ),
            adj=get_adj_expr(mt.GT, mt.GQ, mt.DP, mt.AD),
        )

        # Temporary hotfix for depletion of homozygous alternate genotypes
        logger.info(
            "Setting het genotypes at sites with >1% AF (using v3.0 frequencies) and > 0.9 AB to homalt..."
        )
        # Load v3.0 allele frequencies to avoid an extra frequency calculation
        # NOTE: Using previous callset AF works for small incremental changes to a callset, but we will need to revisit for large increments
        freq_ht = get_freq(version="3").ht()
        mt = hom_alt_depletion_fix(
            mt, het_non_ref_expr=mt.het_non_ref, af_expr=freq_ht[mt.row_key].freq[0].AF
        )

        logger.info("Generating frequency data...")
        if subsets:
            mt = annotate_freq(
                mt,
                sex_expr=mt.meta.sex_imputation.sex_karyotype,
                pop_expr=mt.meta.population_inference.pop
                if not n_subsets_use_subpops
                else mt.meta.project_meta.project_subpop,
                # NOTE: TGP and HGDP labeled populations are highly specific and are stored in the project_subpop meta field
            )
            freq_meta = [
                {**x, **{"subset": "|".join(subsets)}} for x in hl.eval(mt.freq_meta)
            ]
            mt = mt.annotate_globals(freq_meta=freq_meta)

            # NOTE: no FAFs or popmax needed for subsets
            mt = mt.select_rows("freq")
            if n_subsets_use_subpops:
                POPS = POPS_STORED_AS_SUBPOPS
            mt = mt.annotate_globals(
                freq_index_dict=make_freq_index_dict(
                    freq_meta=freq_meta, pops=POPS, label_delimiter="-"
                )
            )
            mt = mt.annotate_rows(freq=set_female_y_metrics_to_na_expr(mt))

            logger.info(
                f"Applying v3.1.2 patch to v3.1 frequencies for {', '.join(subsets)} subset(s)..."
            )

            # Using v3.1 allele frequency HT
            ht = get_freq(subset="_".join(subsets)).versions["3.1"].ht()
            freq_ht = mt.rows()
            ht = ht.annotate(
                freq=hl.if_else(
                    hl.is_defined(freq_ht[ht.key]), freq_ht[ht.key].freq, ht.freq
                )
            )

            logger.info(
                f"Writing out frequency data for {', '.join(subsets)} subset(s)..."
            )
            if args.test:
                freq_ht.write(
                    get_checkpoint_path(
                        f"chr20_test_gnomad_genomes_v3.1.2.patch.frequencies.{'_'.join(subsets)}"
                    ),
                    overwrite=True,
                )
                ht.write(
                    get_checkpoint_path(
                        f"chr20_test_gnomad_genomes_v3.1.2.frequencies.{'_'.join(subsets)}"
                    ),
                    overwrite=True,
                )
            else:
                freq_ht.write(
                    f"gs://gnomad/annotations/hail-0.2/ht/genomes_v3.1.2/gnomad_genomes_v3.1.2.patch.frequencies.{'_'.join(subsets)}.ht",
                    overwrite=args.overwrite,
                )
                ht.write(
                    f"gs://gnomad/annotations/hail-0.2/ht/genomes_v3.1.2/gnomad_genomes_v3.1.2.frequencies.{'_'.join(subsets)}.ht",
                    overwrite=args.overwrite,
                )

        else:
            logger.info("Computing age histograms for each variant...")
            mt = mt.annotate_cols(
                age=hl.if_else(
                    hl.is_defined(mt.meta.project_meta.age),
                    mt.meta.project_meta.age,
                    mt.meta.project_meta.age_alt,
                    # NOTE: most age data is stored as integers in 'age' annotation, but for a select number of samples,
                    # age is stored as a bin range and 'age_alt' corresponds to an integer in the middle of the bin
                )
            )
            mt = mt.annotate_rows(**age_hists_expr(mt.adj, mt.GT, mt.age))

            # Compute callset-wide age histogram global
            mt = mt.annotate_globals(
                age_distribution=mt.aggregate_cols(hl.agg.hist(mt.age, 30, 80, 10))
            )

            mt = annotate_freq(
                mt,
                sex_expr=mt.meta.sex_imputation.sex_karyotype,
                pop_expr=mt.meta.population_inference.pop,
                downsamplings=DOWNSAMPLINGS,
            )
            # Remove all loci with raw AC=0
            mt = mt.filter_rows(mt.freq[1].AC > 0)

            mt = mt.annotate_globals(
                freq_index_dict=make_freq_index_dict(
                    freq_meta=hl.eval(mt.freq_meta),
                    downsamplings=hl.eval(mt.downsamplings),
                    label_delimiter="-",
                )
            )
            mt = mt.annotate_rows(freq=set_female_y_metrics_to_na_expr(mt))

            logger.info("Calculating InbreedingCoeff...")
            # NOTE: This is not the ideal location to calculate this, but added here to avoid another densify
            mt = mt.annotate_rows(
                InbreedingCoeff=bi_allelic_site_inbreeding_expr(mt.GT)
            )

            logger.info("Computing filtering allele frequencies and popmax...")
            faf, faf_meta = faf_expr(
                mt.freq, mt.freq_meta, mt.locus, POPS_TO_REMOVE_FOR_POPMAX
            )
            mt = mt.select_rows(
                "InbreedingCoeff",
                "freq",
                faf=faf,
                popmax=pop_max_expr(mt.freq, mt.freq_meta, POPS_TO_REMOVE_FOR_POPMAX),
            )
            mt = mt.annotate_globals(
                faf_meta=faf_meta,
                faf_index_dict=make_faf_index_dict(faf_meta, label_delimiter="-"),
            )
            mt = mt.annotate_rows(
                popmax=mt.popmax.annotate(
                    faf95=mt.faf[
                        mt.faf_meta.index(
                            lambda x: x.values() == ["adj", mt.popmax.pop]
                        )
                    ].faf95
                )
            )

            logger.info("Annotating quality metrics histograms...")
            # NOTE: these are performed here as the quality metrics histograms also require densifying
            mt = mt.annotate_rows(
                qual_hists=qual_hist_expr(mt.GT, mt.GQ, mt.DP, mt.AD, mt.adj)
            )
            ht = mt.rows()
            ht = ht.annotate(
                qual_hists=hl.Struct(
                    **{
                        i.replace("_adj", ""): ht.qual_hists[i]
                        for i in ht.qual_hists
                        if "_adj" in i
                    }
                ),
                raw_qual_hists=hl.Struct(
                    **{i: ht.qual_hists[i] for i in ht.qual_hists if "_adj" not in i}
                ),
            )

            logger.info("Writing out frequency data for the patch...")
            if args.test:
                ht = ht.checkpoint(
                    get_checkpoint_path(
                        "chr20_test_gnomad_genomes_v3.1.2.patch.frequencies"
                    ),
                    overwrite=True,
                )
            else:
                ht = ht.checkpoint(
                    "gs://gnomad/annotations/hail-0.2/ht/genomes_v3.1.2/gnomad_genomes_v3.1.2.patch.frequencies.ht",
                    overwrite=args.overwrite,
                )

            logger.info(f"Applying v3.1.2 patch to v3.1 frequencies...")

            # Using v3.1 allele frequency HT
            v3_1_freq_ht = get_freq().versions["3.1"].ht()
            ht = v3_1_freq_ht.annotate(
                **{
                    x: hl.if_else(
                        hl.is_defined(ht[v3_1_freq_ht.key]),
                        ht[v3_1_freq_ht.key][x],
                        v3_1_freq_ht[x],
                    )
                    for x in [
                        "freq",
                        "InbreedingCoeff",
                        "faf",
                        "popmax",
                        "qual_hists",
                        "raw_qual_hists",
                    ]
                }
            )

            logger.info("Writing out frequency data...")
            if args.test:
                ht.write(
                    get_checkpoint_path("chr20_test_gnomad_genomes_v3.1.2.frequencies"),
                    overwrite=True,
                )
            else:
                ht.write(
                    "gs://gnomad/annotations/hail-0.2/ht/genomes_v3.1.2/gnomad_genomes_v3.1.2.frequencies.ht",
                    overwrite=args.overwrite,
                )

    finally:
        logger.info("Copying hail log to logging bucket...")
        hl.copy_log(f"{qc_temp_prefix(version='3.1.2')}logs/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test", help="Runs a test on two partitions of chr20.", action="store_true"
    )
    parser.add_argument(
        "--subsets",
        help="List of subsets for which to generate combined frequency data.",
        nargs="*",
        choices=SUBSETS,
    )
    parser.add_argument(
        "--overwrite", help="Overwrites existing files.", action="store_true"
    )
    parser.add_argument(
        "--slack_channel", help="Slack channel to post results and notifications to."
    )
    args = parser.parse_args()

    if args.slack_channel:
        with slack_notifications(slack_token, args.slack_channel):
            main(args)
    else:
        main(args)
