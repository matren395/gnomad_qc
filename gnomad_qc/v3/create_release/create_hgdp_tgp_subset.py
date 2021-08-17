import argparse
import logging

import hail as hl

from gnomad.resources.grch38.gnomad import POPS_STORED_AS_SUBPOPS
from gnomad.resources.grch38.reference_data import (
    dbsnp,
    lcr_intervals,
    seg_dup_intervals,
    telomeres_and_centromeres,
)
from gnomad.resources.resource_utils import DataException
from gnomad.sample_qc.sex import adjusted_sex_ploidy_expr
from gnomad.utils.annotations import get_adj_expr, region_flag_expr
from gnomad.utils.file_utils import file_exists
from gnomad.utils.release import make_freq_index_dict
from gnomad.utils.slack import slack_notifications
from gnomad.utils.vcf import (
    AS_FIELDS,
    SITE_FIELDS,
    SPARSE_ENTRIES,
)

from gnomad_qc.slack_creds import slack_token
from gnomad_qc.v3.resources.annotations import (
    analyst_annotations,
    get_freq,
    get_info,
    vep,
)
from gnomad_qc.v3.resources.basics import get_gnomad_v3_mt
from gnomad_qc.v3.resources.meta import meta
from gnomad_qc.v3.resources.release import (
    release_sites,
    hgdp_1kg_subset,
    hgdp_1kg_subset_annotations,
    hgdp_1kg_subset_sample_tsv,
)
from gnomad_qc.v3.resources.sample_qc import (
    hgdp_tgp_meta,
    hgdp_tgp_pcs,
    hgdp_tgp_pop_outliers,
    hgdp_tgp_relatedness,
    hgdp_tgp_related_samples_to_drop,
)
from gnomad_qc.v3.resources.variant_qc import final_filter, SYNDIP
from gnomad_qc.v3.utils import hom_alt_depletion_fix

logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger("create_subset")
logger.setLevel(logging.INFO)

AS_FIELDS.remove("InbreedingCoeff")

GLOBAL_SAMPLE_ANNOTATION_DICT = hl.struct(
    gnomad_sex_imputation_ploidy_cutoffs=hl.struct(
        Description=(
            "Contains sex chromosome ploidy cutoffs used when determining sex chromosome karyotypes for the gnomAD sex imputation. Format: (upper cutoff for single X, (lower cutoff for double X, upper cutoff for double X), lower cutoff for triple X) and (lower cutoff for single Y, upper cutoff for single Y), lower cutoff for double Y)."
        )
    ),
    gnomad_population_inference_pca_metrics=hl.struct(
        Description=(
            "Contains the number of principal components (PCs) used when running PC-project and the minimum cutoff probability of belonging to a given population for the gnomAD population inference."
        )
    ),
    sample_hard_filter_cutoffs=hl.struct(
        Description=(
            "Contains the cutoffs used for hard-filtering samples prior to sample QC. Sample QC metrics are computed using the Hail sample_qc module on all autosomal bi-allelic SNVs. Samples are removed if they are clear outliers for any of the following metrics: number of snps (n_snp), ratio of heterozygous variants to homozygous variants (r_het_hom_var), number of singletons (n_singleton), and mean coverage on chromosome 20 (cov). Additionally, we filter based on outliers of the following BAM/CRAM-derived metrics: % contamination (freemix), % chimera, and median insert size."
        )
    ),
    gnomad_sample_qc_metric_outlier_cutoffs=hl.struct(
        Description=(
            "Contains the cutoffs used for filtering outlier samples based on QC metrics (reported in the sample_qc and gnomad_sample_qc_residuals annotations). The first eight PCs computed during the gnomAD ancestry assignment were regressed out and the sample filter cutoffs were determined based on the residuals for each of the sample QC metrics. Samples were filtered if they fell outside four median absolute deviations (MADs) from the median for the following sample QC metrics: n_snp, r_ti_tv, r_insertion_deletion, n_insertion, n_deletion, n_het, n_hom_var, n_transition, and n_transversion. Samples over 8 MADs above the median n_singleton metric and over 4 MADs above the median r_het_hom_var metric were also filtered."
        )
    ),
    gnomad_age_distribution=hl.struct(
        Description="gnomAD callset-wide age histogram calculated on release samples.",
        sub_globals=hl.struct(
            bin_edges = hl.struct(
                Description="Bin edges for the age histogram."
            ),
            bin_freq = hl.struct(
                Description="Bin frequencies for the age histogram. This is the number of records found in each bin."
            ),
            n_smaller = hl.struct(
                Description="Count of age values falling below lowest histogram bin edge."
            ),
            n_larger = hl.struct(
                Description="Count of age values falling above highest histogram bin edge."
            )
        ),
    ),
)
GLOBAL_VARIANT_ANNOTATION_DICT = hl.struct(
    hgdp_tgp_freq_meta=hl.struct(
        Description=(
            "HGDP and 1KG frequency metadata. An ordered list containing the frequency aggregation group for each element of the hgdp_tgp_freq array row annotation."
        )
    ),
    gnomad_freq_meta=hl.struct(
        Description=(
            "gnomAD frequency metadata. An ordered list containing the frequency aggregation group for each element of the gnomad_freq array row annotation."
        )
    ),
    hgdp_tgp_freq_index_dict=hl.struct(
        Description=(
            "Dictionary keyed by specified label grouping combinations (group: adj/raw, pop: HGDP or 1KG subpopulation, sex: sex karyotype), with values describing the corresponding index of each grouping entry in the HGDP + 1KG frequency array annotation."
        )
    ),
    gnomad_freq_index_dict=hl.struct(
        Description=(
            "Dictionary keyed by specified label grouping combinations (group: adj/raw, pop: gnomAD inferred global population sex: sex karyotype), with values describing the corresponding index of each grouping entry in the gnomAD frequency array annotation."
        )
    ),
    gnomad_faf_meta=hl.struct(
        Description=(
            "gnomAD filtering allele frequency metadata. An ordered list containing the frequency aggregation group for each element of the gnomad_faf array row annotation."
        )
    ),
    gnomad_faf_index_dict=hl.struct(
        Description=(
            "Dictionary keyed by specified label grouping combinations (group: adj/raw, pop: gnomAD inferred global population sex: sex karyotype), with values describing the corresponding index of each grouping entry in the filtering allele frequency (using Poisson 99% CI) annotation."
        )
    ),
    variant_filtering_model=hl.struct(
        Description="The variant filtering model used and its specific cutoffs.",
        sub_globals=hl.struct(
            model_name=hl.struct(
                Description=(
                    "Variant filtering model name used in the 'filters' row annotation to indicate the variant was filtered by the model during variant QC."
                )
            ),
            score_name=hl.struct(
                Description="Name of score used for variant filtering."
            ),
            snv_cutoff=hl.struct(
                Description="SNV filtering cutoff information.",
                sub_globals=hl.struct(
                    bin=hl.struct(Description="Filtering percentile cutoff for SNVs."),
                    min_score=hl.struct(
                        Description="Minimum score at SNV filtering percentile cutoff."
                    ),
                ),
            ),
            indel_cutoff=hl.struct(
                Description="Information about cutoff used for indel filtering.",
                sub_globals=hl.struct(
                    bin=hl.struct(
                        Description="Filtering percentile cutoff for indels."
                    ),
                    min_score=hl.struct(
                        Description="Minimum score at indel filtering percentile cutoff."
                    ),
                ),
            ),
            snv_training_variables=hl.struct(
                Description="Variant annotations used as features in SNV filtering model."
            ),
            indel_training_variables=hl.struct(
                Description="Variant annotations used as features in indel filtering model."
            ),
        ),
    ),
    variant_inbreeding_coeff_cutoff=hl.struct(
        Description="Hard-filter cutoff for InbreedingCoeff on variants."
    ),
    vep_version=hl.struct(Description="VEP version."),
    vep_csq_header=hl.struct(Description="VEP header for VCF export."),
    dbsnp_version=hl.struct(Description="dbSNP version."),
)
GLOBAL_ANNOTATION_DICT = hl.struct(
    **GLOBAL_SAMPLE_ANNOTATION_DICT, **GLOBAL_VARIANT_ANNOTATION_DICT
)

SAMPLE_ANNOTATION_DICT = hl.struct(
    s=hl.struct(Description="Sample ID."),
    bam_metrics=hl.struct(
        Description="Sample level metrics obtained from BAMs/CRAMs.",
        sub_annotations=hl.struct(
            pct_bases_20x=hl.struct(
                Description="The fraction of bases that attained at least 20X sequence coverage in post-filtering bases."
            ),
            pct_chimeras=hl.struct(
                Description=(
                    "The fraction of reads that map outside of a maximum insert size (usually 100kb) or that have the two ends mapping to different chromosomes."
                )
            ),
            freemix=hl.struct(Description="Estimate of contamination (0-100 scale)."),
            mean_coverage=hl.struct(
                Description="The mean coverage in bases of the genome territory after all filters are applied; see: https://broadinstitute.github.io/picard/picard-metric-definitions.html."
            ),
            median_coverage=hl.struct(
                Description="The median coverage in bases of the genome territory after all filters are applied; see: https://broadinstitute.github.io/picard/picard-metric-definitions.html."
            ),
            mean_insert_size=hl.struct(
                Description=(
                    "The mean insert size of the 'core' of the distribution. Artefactual outliers in the distribution often cause calculation of nonsensical mean and stdev values. To avoid this, the distribution is first trimmed to a 'core' distribution of +/- N median absolute deviations around the median insert size."
                )
            ),
            median_insert_size=hl.struct(
                Description="The median insert size of all paired end reads where both ends mapped to the same chromosome."
            ),
            pct_bases_10x=hl.struct(
                Description="The fraction of bases that attained at least 10X sequence coverage in post-filtering bases."
            ),
        ),
    ),
    sample_qc=hl.struct(
        Description="Struct containing sample QC metrics calculated using hl.sample_qc().",
        sub_annotations=hl.struct(
            n_deletion=hl.struct(Description="Number of deletion alternate alleles."),
            n_het=hl.struct(Description="Number of heterozygous calls."),
            n_hom_ref=hl.struct(Description="Number of homozygous reference calls."),
            n_hom_var=hl.struct(Description="Number of homozygous alternate calls."),
            n_insertion=hl.struct(Description="Number of insertion alternate alleles."),
            n_non_ref=hl.struct(Description="Sum of n_het and n_hom_var."),
            n_snp=hl.struct(Description="Number of SNP alternate alleles."),
            n_transition=hl.struct(
                Description="Number of transition (A-G, C-T) alternate alleles."
            ),
            n_transversion=hl.struct(
                Description="Number of transversion alternate alleles."
            ),
            r_het_hom_var=hl.struct(Description="Het/HomVar call ratio."),
            r_insertion_deletion=hl.struct(
                Description="Insertion/Deletion allele ratio."
            ),
            r_ti_tv=hl.struct(Description="Transition/Transversion ratio."),
        ),
    ),
    gnomad_sex_imputation=hl.struct(
        Description="Struct containing sex imputation information.",
        sub_annotations=hl.struct(
            chr20_mean_dp=hl.struct(
                Description="Sample's mean depth across chromosome 20."
            ),
            chrX_mean_dp=hl.struct(
                Description="Sample's mean depth across chromosome X."
            ),
            chrY_mean_dp=hl.struct(
                Description="Sample's mean depth across chromosome Y."
            ),
            chrX_ploidy=hl.struct(
                Description="Sample's chromosome X ploidy (chrX_mean_dp normalized using chr20_mean_dp)."
            ),
            chrY_ploidy=hl.struct(
                Description="Sample's chromosome Y ploidy (chrY_mean_dp normalized using chr20_mean_dp)."
            ),
            X_karyotype=hl.struct(Description="Sample's chromosome X karyotype."),
            Y_karyotype=hl.struct(Description="Sample's chromosome Y karyotype."),
            sex_karyotype=hl.struct(
                Description="Sample's sex karyotype (combined X and Y karyotype)."
            ),
            f_stat=hl.struct(
                Description="Inbreeding coefficient (excess heterozygosity) on chromosome X."
            ),
            n_called=hl.struct(Description="Number of variants with a genotype call."),
            expected_homs=hl.struct(Description="Expected number of homozygotes."),
            observed_homs=hl.struct(Description="Observed number of homozygotes."),
        ),
    ),
    gnomad_population_inference=hl.struct(
        Description=(
            "Struct containing ancestry information assigned by applying a principal components analysis (PCA) on gnomAD samples and using those PCs in a random forest classifier trained on known gnomAD ancestry labels."
        ),
        sub_annotations=hl.struct(
            pca_scores=hl.struct(
                Description="Sample's scores for each gnomAD population PC."
            ),
            pop=hl.struct(Description="Sample's inferred gnomAD population label."),
            prob_afr=hl.struct(
                Description="Random forest probability that the sample is of African/African-American ancestry."
            ),
            prob_ami=hl.struct(
                Description="Random forest probability that the sample is of Amish ancestry."
            ),
            prob_amr=hl.struct(
                Description="Random forest probability that the sample is of Latino ancestry."
            ),
            prob_asj=hl.struct(
                Description="Random forest probability that the sample is of Ashkenazi Jewish ancestry."
            ),
            prob_eas=hl.struct(
                Description="Random forest probability that the sample is of East Asian ancestry."
            ),
            prob_fin=hl.struct(
                Description="Random forest probability that the sample is of Finnish ancestry."
            ),
            prob_mid=hl.struct(
                Description="Random forest probability that the sample is of Middle Eastern ancestry."
            ),
            prob_nfe=hl.struct(
                Description="Random forest probability that the sample is of Non-Finnish European ancestry."
            ),
            prob_oth=hl.struct(
                Description="Random forest probability that the sample is of Other ancestry."
            ),
            prob_sas=hl.struct(
                Description="Random forest probability that the sample is of South Asian ancestry."
            ),
        ),
    ),
    gnomad_sample_qc_residuals=hl.struct(
        Description=(
            "Struct containing the residuals after regressing out the first eight PCs computed during the gnomAD ancestry assignment from each sample QC metric calculated using hl.sample_qc()."
        ),
        sub_annotations=hl.struct(
            n_deletion_residual=hl.struct(
                Description=(
                    "Residuals after regressing out the first eight ancestry PCs from the number of deletion alternate alleles."
                )
            ),
            n_insertion_residual=hl.struct(
                Description=(
                    "Residuals after regressing out the first eight ancestry PCs from the number of insertion alternate alleles."
                ),
            ),
            n_snp_residual=hl.struct(
                Description=(
                    "Residuals after regressing out the first eight ancestry PCs from the number of SNP alternate alleles."
                ),
            ),
            n_transition_residual=hl.struct(
                Description=(
                    "Residuals after regressing out the first eight ancestry PCs from the number of transition (A-G, C-T) alternate alleles."
                )
            ),
            n_transversion_residual=hl.struct(
                Description=(
                    "Residuals after regressing out the first eight ancestry PCs from the number of transversion alternate alleles."
                )
            ),
            r_het_hom_var_residual=hl.struct(
                Description=(
                    "Residuals after regressing out the first eight ancestry PCs from the Het/HomVar call ratio."
                ),
            ),
            r_insertion_deletion_residual=hl.struct(
                Description=(
                    "Residuals after regressing out the first eight ancestry PCs from the Insertion/Deletion allele ratio."
                )
            ),
            r_ti_tv_residual=hl.struct(
                Description=(
                    "Residuals after regressing out the first eight ancestry PCs from the Transition/Transversion ratio."
                )
            ),
        ),
    ),
    gnomad_sample_filters=hl.struct(
        Description="Sample QC filter annotations used for the gnomAD release.",
        sub_annotations=hl.struct(
            hard_filters=hl.struct(
                Description=(
                    "Set of hard filters applied to each sample prior to additional sample QC. Samples are hard filtered if they are extreme outliers for any of the following metrics: number of snps (n_snp), ratio of heterozygous variants to homozygous variants (r_het_hom_var), number of singletons (n_singleton), and mean coverage on chromosome 20 (cov). Additionally, we filter based on outliers of the following Picard metrics: % contamination (freemix), % chimera, and median insert size."
                )
            ),
            hard_filtered=hl.struct(
                Description=(
                    "Whether a sample was hard filtered. The gnomad_sample_filters.hard_filters set is empty if this annotation is True."
                )
            ),
            release_related=hl.struct(
                Description=(
                    "Whether a sample had a second-degree or greater relatedness to another sample in the gnomAD release."
                )
            ),
            qc_metrics_filters=hl.struct(
                Description=(
                    "Set of all sample QC metrics for which each sample was found to be an outlier after computing sample QC metrics using the Hail sample_qc() module and regressing out the first 8 gnomAD ancestry assignment PCs."
                )
            ),
        ),
    ),
    gnomad_high_quality=hl.struct(
        Description=(
            "Whether a sample has passed gnomAD sample QC metrics except for relatedness (i.e., gnomad_sample_filters.hard_filters and gnomad_sample_filters.qc_metrics_filters are empty sets)."
        )
    ),
    gnomad_release=hl.struct(
        Description=(
            "Whether the sample was included in the gnomAD release dataset. For the full gnomAD release, relatedness inference is performed on the full dataset, and release samples are chosen in a way that maximizes the number of samples retained while filtering the dataset to include only samples with less than second-degree relatedness. For the HGDP + 1KG subset, samples passing all other sample QC metrics are retained."
        )
    ),
    relatedness_inference=hl.struct(
        Description="",
        sub_annotations=hl.struct(
            related_samples=hl.struct(
                Description="",
                sub_annotations=hl.struct(
                    s="Sample ID.",
                    kin="Kinship estimate.",
                    ibd0="IBD0 estimate.",
                    ibd1="IBD1 estimate.",
                    ibd2="IBD2 estimate.",
                ),
            ),
            related=hl.struct(Description=""),
        ),
    ),
    hgdp_tgp_meta=hl.struct(
        Description="",
        sub_annotations=hl.struct(
            project=hl.struct(
                Description=""
            ),
            study_region=hl.struct(
                Description=""
            ),
            population=hl.struct(
                Description=""
            ),
            geographic_region=hl.struct(
                Description=""
            ),
            latitude=hl.struct(Description=""),
            longitude=hl.struct(Description=""),
            hgdp_technical_meta=hl.struct(
                Description=(
                    "Technical considerations for HGDP detailed in https://science.sciencemag.org/content/367/6484/eaay5012/"
                ),
                sub_annotations=hl.struct(
                    source=hl.struct(
                        Description="Which batch/project these HGDP samples were sequenced as part of (sanger vs sgdp)."
                    ),
                    library_type=hl.struct(
                        Description="Whether samples were PCRfree or used PCR."
                    ),
                ),
            ),
            global_pca_scores=hl.struct(Description=""),
            subcontinental_pca=hl.struct(
                Description="",
                sub_annotations=hl.struct(
                    pca_scores=hl.struct(
                        Description=""
                    ),
                    pca_scores_outliers_removed=hl.struct(
                        Description=""
                    ),
                    outlier=hl.struct(
                        Description=""
                    ),
                ),
            ),
            gnomad_labeled_subpop=hl.struct(Description=""),
        ),
    ),
    high_quality=hl.struct(
        Description="Samples that pass all ‘gnomad_sample_filters.hard_filters’ and were not found to be outliers in global population-specific principal component analysis hgdp_tgp_meta.subcontinental_pca.outlier"
    ),
)

VARIANT_ANNOTATION_DICT = hl.struct(
    locus=hl.struct(
        Description="Variant locus. Contains contig and position information.",
    ),
    alleles=hl.struct(
        Description="Variant alleles.",
    ),
    rsid=hl.struct(
        Description="dbSNP reference SNP identification (rsID) numbers.",
    ),
    a_index=hl.struct(
        Description="The original index of this alternate allele in the multiallelic representation (1 is the first alternate allele or the only alternate allele in a biallelic variant)."
    ),
    was_split=hl.struct(
        Description="True if this variant was originally multiallelic, otherwise False."
    ),
    hgdp_tgp_freq=hl.struct(
        Description="Allele frequency information (AC, AN, AF, homozygote count) in HGDP + 1KG samples that pass the high_quality sample annotation and are inferred as unrelated (False in relatedness_inference.related annotation).",
        sub_annotations = hl.struct(
            AC=hl.struct(
                Description="Alternate allele count  in HGDP + 1KG samples that pass the high_quality sample annotation."
            ),
            AF=hl.struct(
                Description="Alternate allele frequency  in HGDP + 1KG samples that pass the high_quality sample annotation."
            ),
            AN=hl.struct(
                Description="Total number of alleles in HGDP + 1KG samples that pass the high_quality sample annotation."
            ),
            homozygote_count=hl.struct(
                Description="Count of homozygous individuals in HGDP + 1KG samples that pass the high_quality sample annotation."
            ),
        ),
    ),
    gnomad_freq=hl.struct(
        Description="Allele frequency information (AC, AN, AF, homozygote count) in gnomAD release.",
        sub_annotations = hl.struct(
            AC=hl.struct(
                Description="Alternate allele count in gnomAD release."
            ),
            AF=hl.struct(
                Description="Alternate allele frequency in gnomAD release."
            ),
            AN=hl.struct(
                Description="Total number of alleles in gnomAD release."
            ),
            homozygote_count=hl.struct(
                Description="Count of homozygous individuals in gnomAD release."
            ),
        ),
    ),
    gnomad_popmax=hl.struct(
        Description="Allele frequency information (AC, AN, AF, homozygote count) for the population with maximum AF in gnomAD.",
        sub_annotations=hl.struct(
            AC=hl.struct(
                Description="Allele count in the population with the maximum AF in gnomAD."
            ),
            AF=hl.struct(
                Description="Maximum allele frequency across populations in gnomAD."
            ),
            AN=hl.struct(
                Description="Total number of alleles in the population with the maximum AF in gnomAD."
            ),
            homozygote_count=hl.struct(
                Description="Count of homozygous individuals in the population with the maximum allele frequency in gnomAD."
            ),
            pop=hl.struct(
                Description="Population with maximum AF in gnomAD."
            ),
            faf95=hl.struct(
                Description="Filtering allele frequency (using Poisson 95% CI) for the population with the maximum allele frequency in gnomAD."
            ),
        ),
    ),
    gnomad_faf=hl.struct(
        Description="Filtering allele frequency in gnomAD release.",
        sub_annotations = hl.struct(
            faf95=hl.struct(
                Description="Filtering allele frequency in gnomAD release (using Poisson 95% CI)."
            ),
            faf99=hl.struct(
                Description="Filtering allele frequency in gnomAD release (using Poisson 99% CI)."
            ),
        ),
    ),
    gnomad_qual_hists=hl.struct(
        Description="gnomAD genotype quality metric histograms for high quality genotypes.",
        sub_annotations=hl.struct(
            gq_hist_all=hl.struct(
                Description="Histogram for GQ calculated on high quality genotypes.",
                sub_annotations=hl.struct(
                    bin_edges=hl.struct(
                        Description="Bin edges for the GQ histogram calculated on high quality genotypes are: 0|5|10|15|20|25|30|35|40|45|50|55|60|65|70|75|80|85|90|95|100",
                    ),
                    bin_freq=hl.struct(
                        Description="Bin frequencies for the GQ histogram calculated on high quality genotypes. The number of records found in each bin."
                    ),
                    n_smaller=hl.struct(
                        Description="Count of GQ values falling below lowest histogram bin edge, for GQ calculated on high quality genotypes",
                    ),
                    n_larger=hl.struct(
                        Description="Count of GQ values falling above highest histogram bin edge, for GQ calculated on high quality genotypes",
                    ),
                ),
            ),
            dp_hist_all=hl.struct(
                Description="Histogram for DP calculated on high quality genotypes.",
                sub_annotations=hl.struct(
                    bin_edges=hl.struct(
                        Description="Bin edges for the DP histogram calculated on high quality genotypes are: 0|5|10|15|20|25|30|35|40|45|50|55|60|65|70|75|80|85|90|95|100."
                    ),
                    bin_freq=hl.struct(
                        Description="Bin frequencies for the DP histogram calculated on high quality genotypes. The number of records found in each bin."
                    ),
                    n_smaller=hl.struct(
                        Description="Count of DP values falling below lowest histogram bin edge, for DP calculated on high quality genotypes."
                    ),
                    n_larger=hl.struct(
                        Description="Count of DP values falling above highest histogram bin edge, for DP calculated on high quality genotypes."
                    ),
                ),
            ),
            gq_hist_alt=hl.struct(
                Description="Histogram for GQ in heterozygous individuals calculated on high quality genotypes.",
                sub_annotations=hl.struct(
                    bin_edges=hl.struct(
                        Description="Bin edges for the histogram of GQ in heterozygous individuals calculated on high quality genotypes are: 0|5|10|15|20|25|30|35|40|45|50|55|60|65|70|75|80|85|90|95|100.",
                    ),
                    bin_freq=hl.struct(
                        Description="Bin frequencies for the histogram of GQ in heterozygous individuals calculated on high quality genotypes. The number of records found in each bin."
                    ),
                    n_smaller=hl.struct(
                        Description="Count of GQ values falling below lowest histogram bin edge, for GQ in heterozygous individuals calculated on high quality genotypes."
                    ),
                    n_larger=hl.struct(
                        Description="Count of GQ values falling above highest histogram bin edge, for GQ in heterozygous individuals calculated on high quality genotypes."
                    ),
                ),
            ),
            dp_hist_alt=hl.struct(
                Description="Histogram for DP in heterozygous individuals calculated on high quality genotypes.",
                sub_annotations=hl.struct(
                    bin_edges=hl.struct(
                        Description="Bin edges for the histogram of DP in heterozygous individuals calculated on high quality genotypes are: 0|5|10|15|20|25|30|35|40|45|50|55|60|65|70|75|80|85|90|95|100."
                    ),
                    bin_freq=hl.struct(
                        Description="Bin frequencies for the histogram of DP in heterozygous individuals calculated on high quality genotypes. The number of records found in each bin."
                    ),
                    n_smaller=hl.struct(
                        Description="Count of DP values falling below lowest histogram bin edge, for DP in heterozygous individuals calculated on high quality genotypes."
                    ),
                    n_larger=hl.struct(
                        Description="Count of DP values falling above highest histogram bin edge, for DP in heterozygous individuals calculated on high quality genotypes."
                    ),
                ),
            ),
            ab_hist_alt=hl.struct(
                Description="Histogram for AB in heterozygous individuals calculated on high quality genotypes.",
                sub_annotations=hl.struct(
                    bin_edges=hl.struct(
                        Description="Bin edges for the histogram of AB in heterozygous individuals calculated on high quality genotypes are: 0.00|0.05|0.10|0.15|0.20|0.25|0.30|0.35|0.40|0.45|0.50|0.55|0.60|0.65|0.70|0.75|0.80|0.85|0.90|0.95|1.00."
                    ),
                    bin_freq=hl.struct(
                        Description="Bin frequencies for the histogram of AB in heterozygous individuals calculated on high quality genotypes. The number of records found in each bin."
                    ),
                    n_smaller=hl.struct(
                        Description="Count of AB values falling below lowest histogram bin edge, for AB in heterozygous individuals calculated on high quality genotypes."
                    ),
                    n_larger=hl.struct(
                        Description="Count of AB values falling above highest histogram bin edge, for AB in heterozygous individuals calculated on high quality genotypes."
                    ),
                ),
            ),
        ),
    gnomad_raw_qual_hists=hl.struct(
        Description="gnomAD genotype quality metric histograms.",
        sub_annotations=hl.struct(
            gq_hist_all=hl.struct(
                Description="Histogram for GQ calculated on all genotypes.",
                sub_annotations=hl.struct(
                    bin_edges=hl.struct(
                        Description="Bin edges for the GQ histogram calculated on all genotypes are: 0|5|10|15|20|25|30|35|40|45|50|55|60|65|70|75|80|85|90|95|100."
                    ),
                    bin_freq=hl.struct(
                        Description="Bin frequencies for the GQ histogram calculated on all genotypes. The number of records found in each bin."
                    ),
                    n_smaller=hl.struct(
                        Description="Count of GQ values falling below lowest histogram bin edge, for GQ calculated on all genotypes."
                    ),
                    n_larger=hl.struct(
                        Description="Count of GQ values falling above highest histogram bin edge, for GQ calculated on all genotypes."
                    ),
                ),
            ),
            dp_hist_all=hl.struct(
                Description="Histogram for DP calculated on all genotypes.",
                sub_annotations=hl.struct(
                    bin_edges=hl.struct(
                        Description="Bin edges for the DP histogram calculated on all genotypes are: 0|5|10|15|20|25|30|35|40|45|50|55|60|65|70|75|80|85|90|95|100"
                    ),
                    bin_freq=hl.struct(
                        Description="Bin frequencies for the DP histogram calculated on all genotypes. The number of records found in each bin."
                    ),
                    n_smaller=hl.struct(
                        Description="Count of DP values falling below lowest histogram bin edge, for DP calculated on all genotypes."
                    ),
                    n_larger=hl.struct(
                        Description="Count of DP values falling above highest histogram bin edge, for DP calculated on all genotypes."
                    ),
                ),
            ),
            gq_hist_alt=hl.struct(
                Description="Histogram for GQ in heterozygous individuals calculated on all genotypes.",
                sub_annotations=hl.struct(
                    bin_edges=hl.struct(
                        Description="Bin edges for the histogram of GQ in heterozygous individuals calculated on all genotypes are: 0|5|10|15|20|25|30|35|40|45|50|55|60|65|70|75|80|85|90|95|100."
                    ),
                    bin_freq=hl.struct(
                        Description="Bin frequencies for the histogram of GQ in heterozygous individuals calculated on all genotypes. The number of records found in each bin."
                    ),
                    n_smaller=hl.struct(
                        Description="Count of GQ values falling below lowest histogram bin edge, for GQ in heterozygous individuals calculated on all genotypes."
                    ),
                    n_larger=hl.struct(
                        Description="Count of GQ values falling above highest histogram bin edge, for GQ in heterozygous individuals calculated on all genotypes."
                    ),
                ),
            ),
            dp_hist_alt=hl.struct(
                Description="Histogram for DP in heterozygous individuals calculated on all genotypes.",
                sub_annotations=hl.struct(
                    bin_edges=hl.struct(
                        Description="Bin edges for the histogram of DP in heterozygous individuals calculated on all genotypes are: 0|5|10|15|20|25|30|35|40|45|50|55|60|65|70|75|80|85|90|95|100."
                    ),
                    bin_freq=hl.struct(
                        Description="Bin frequencies for the histogram of DP in heterozygous individuals calculated on all genotypes. The number of records found in each bin."
                    ),
                    n_smaller=hl.struct(
                        Description="Count of DP values falling below lowest histogram bin edge, for DP in heterozygous individuals calculated on all genotypes."
                    ),
                    n_larger=hl.struct(
                        Description="Count of DP values falling above highest histogram bin edge, for DP in heterozygous individuals calculated on all genotypes."
                    ),
                ),
            ),
            ab_hist_alt=hl.struct(
                Description="Histogram for AB in heterozygous individuals calculated on all genotypes.",
                sub_annotations=hl.struct(
                    bin_edges=hl.struct(
                        Description="Bin edges for the histogram of AB in heterozygous individuals calculated on all genotypes are: 0.00|0.05|0.10|0.15|0.20|0.25|0.30|0.35|0.40|0.45|0.50|0.55|0.60|0.65|0.70|0.75|0.80|0.85|0.90|0.95|1.00."
                    ),
                    bin_freq=hl.struct(
                        Description="Bin frequencies for the histogram of AB in heterozygous individuals calculated on all genotypes. The number of records found in each bin."
                    ),
                    n_smaller=hl.struct(
                        Description="Count of AB values falling below lowest histogram bin edge, for AB in heterozygous individuals calculated on all genotypes."
                    ),
                    n_larger=hl.struct(
                        Description="Count of AB values falling above highest histogram bin edge, for AB in heterozygous individuals calculated on all genotypes."
                    ),
                ),
            ),
        ),
    ),
    gnomad_age_hist_het=hl.struct(
        Description="Histogram for age in all heterozygous gnomAD release samples calculated on high quality genotypes.",
        sub_annotations=hl.struct(
            bin_edges=hl.struct(
                Description="Bin edges for the age histogram."
            ),
            bin_freq=hl.struct(
                Description="Bin frequencies for the age histogram. This is the number of records found in each bin."
            ),
            n_smaller=hl.struct(
                Description="Count of age values falling below lowest histogram bin edge."
            ),
            n_larger=hl.struct(
                Description="Count of age values falling above highest histogram bin edge."
            ),
        ),
    ),
    gnomad_age_hist_hom=hl.struct(
        Description="Histogram for age in all homozygous gnomAD release samples calculated on high quality genotypes.",
        sub_annotations=hl.struct(
            bin_edges=hl.struct(
                Description="Bin edges for the age histogram."
            ),
            bin_freq=hl.struct(
                Description="Bin frequencies for the age histogram. This is the number of records found in each bin."
            ),
            n_smaller=hl.struct(
                Description="Count of age values falling below lowest histogram bin edge."
            ),
            n_larger=hl.struct(
                Description="Count of age values falling above highest histogram bin edge."
            ),
        ),
    ),
    filters=hl.struct(
        Description="Variant filters; AC0: Allele count is zero after filtering out low-confidence genotypes (GQ < 20; DP < 10; and AB < 0.2 for het calls), AS_VQSR: Failed VQSR filtering thresholds of -2.7739 for SNPs and -1.0606 for indels, InbreedingCoeff: GATK InbreedingCoeff < -0.3, PASS: Passed all variant filters."
    ),
    info=hl.struct(
        Description="Struct containing typical GATK allele-specific (AS) info fields and additional variant QC fields.",
        sub_annotations=hl.struct(
            QUALapprox=hl.struct(
                Description="Sum of PL[0] values; used to approximate the QUAL score."
            ),
            SB=hl.struct(
                Description="Per-sample component statistics which comprise the Fisher's exact test to detect strand bias. Values are: depth of reference allele on forward strand, depth of reference allele on reverse strand, depth of alternate allele on forward strand, depth of alternate allele on reverse strand."
            ),
            MQ=hl.struct(
                Description="Root mean square of the mapping quality of reads across all samples."
            ),
            MQRankSum=hl.struct(
                Description="Z-score from Wilcoxon rank sum test of alternate vs. reference read mapping qualities."
            ),
            VarDP=hl.struct(
                Description="Depth over variant genotypes (does not include depth of reference samples)."
            ),
            AS_ReadPosRankSum=hl.struct(
                Description="Allele-specific z-score from Wilcoxon rank sum test of alternate vs. reference read position bias."
            ),
            AS_pab_max=hl.struct(
                Description="Maximum p-value over callset for binomial test of observed allele balance for a heterozygous genotype, given expectation of 0.5."
            ),
            AS_QD=hl.struct(
                Description="Allele-specific variant call confidence normalized by depth of sample reads supporting a variant."
            ),
            AS_MQ=hl.struct(
                Description="Allele-specific root mean square of the mapping quality of reads across all samples."
            ),
            QD=hl.struct(
                Description="Variant call confidence normalized by depth of sample reads supporting a variant."
            ),
            AS_MQRankSum=hl.struct(
                Description="Allele-specific z-score from Wilcoxon rank sum test of alternate vs. reference read mapping qualities."
            ),
            FS=hl.struct(
                Description="Phred-scaled p-value of Fisher's exact test for strand bias."
            ),
            AS_FS=hl.struct(
                Description="Allele-specific phred-scaled p-value of Fisher's exact test for strand bias."
            ),
            ReadPosRankSum=hl.struct(
                Description="Z-score from Wilcoxon rank sum test of alternate vs. reference read position bias."
            ),
            AS_QUALapprox=hl.struct(
                Description="Allele-specific sum of PL[0] values; used to approximate the QUAL score."
            ),
            AS_SB_TABLE=hl.struct(
                Description="Allele-specific forward/reverse read counts for strand bias tests."
            ),
            AS_VarDP=hl.struct(
                Description="Allele-specific depth over variant genotypes (does not include depth of reference samples)."
            ),
            AS_SOR=hl.struct(
                Description="Allele-specific strand bias estimated by the symmetric odds ratio test."
            ),
            SOR=hl.struct(
                Description="Strand bias estimated by the symmetric odds ratio test."
            ),
            transmitted_singleton=hl.struct(
                Description="Variant was a callset-wide doubleton that was transmitted within a family from a parent to a child (i.e., a singleton amongst unrelated samples in cohort)."
            ),
            omni=hl.struct(
                Description="Variant is present on the Omni 2.5 genotyping array and found in 1000 Genomes data."
            ),
            mills=hl.struct(
                Description="Indel is present in the Mills and Devine data."
            ),
            monoallelic=hl.struct(
                Description="All samples are all homozygous alternate for the variant."
            ),
            InbreedingCoeff=hl.struct(
                Description="Inbreeding coefficient, the excess heterozygosity at a variant site, computed as 1 - (the number of heterozygous genotypes)/(the number of heterozygous genotypes expected under Hardy-Weinberg equilibrium)."
            ),
        ),
    ),
    vep=hl.struct(
        Description="Consequence annotations from Ensembl VEP. More details about VEP output is described here: https://uswest.ensembl.org/info/docs/tools/vep/vep_formats.html#output. VEP was run using the LOFTEE plugin and information about the additional LOFTEE annotations can be found here: https://github.com/konradjk/loftee."
        ),
    ),
    vqsr=hl.struct(
        Description="VQSR related variant annotations.",
        sub_annotations=hl.struct(
            AS_VQSLOD=hl.struct(
                Description="Allele-specific log-odds ratio of being a true variant versus being a false positive under the trained VQSR Gaussian mixture model."
            ),
            AS_culprit=hl.struct(
                Description="Allele-specific worst-performing annotation in the VQSR Gaussian mixture model."
            ),
            NEGATIVE_TRAIN_SITE=hl.struct(
                Description="Variant was used to build the negative training set of low-quality variants for VQSR."
            ),
            POSITIVE_TRAIN_SITE=hl.struct(
                Description="Variant was used to build the positive training set of high-quality variants for VQSR."
            ),
        ),
    ),
    region_flag=hl.struct(
        Description="Struct containing flags for problematic regions.",
        sub_annotations=hl.struct(
            lcr=hl.struct(
                Description="Variant falls within a low complexity region.",
            ),
            segdup=hl.struct(
                Description="Variant falls within a segmental duplication region."
            ),
        ),
    ),
    allele_info=hl.struct(
        Description="Allele information.",
    ),
    variant_type=hl.struct(
        Description="Variant type (snv, indel, multi-snv, multi-indel, or mixed).",
    ),
    allele_type=hl.struct(
        Description="Allele type (snv, insertion, deletion, or mixed).",
    ),
    n_alt_alleles=hl.struct(
        Description="Total number of alternate alleles observed at variant locus.",
    ),
    was_mixed=hl.struct(
        Description="Variant type was mixed.",
    ),
    cadd=hl.struct(
        sub_annotations=hl.struct(
            raw_score=hl.struct(
                Description="Raw CADD scores are interpretable as the extent to which the annotation profile for a given variant suggests that the variant is likely to be 'observed' (negative values) vs 'simulated' (positive values); higher values indicate that a variant is more likely to be simulated (or 'not observed') and therefore more likely to have deleterious effects. More information can be found on the CADD website: https://cadd.gs.washington.edu/info."
            ),
            phred=hl.struct(
                Description="CADD Phred-like scores ('scaled C-scores') ranging from 1 to 99, based on the rank of each variant relative to all possible 8.6 billion substitutions in the human reference genome. Larger values are more deleterious. More information can be found on the CADD website: https://cadd.gs.washington.edu/info."
            ),
            has_duplicate=hl.struct(
                Description="a True/False flag that indicates whether the variant has more than one CADD score associated with it*."
            ),
        ),
    ),
    revel=hl.struct(
        Description="dbNSFP's Revel score, ranging from 0 to 1. Variants with higher scores are predicted to be more likely to be deleterious.",
        sub_annotations=hl.struct(
            revel_score=hl.struct(
                Description="Revel’s numerical score from 0 to 1."
            ),
            has_duplicate=hl.struct(
                Description="a True/False flag that indicates whether the variant has more than one revel_score associated with it*."
            ),
        ),
    ),
    splice_ai=hl.struct(
        sub_annotations=hl.struct(
            splice_ai=hl.struct(
                Description="The maximum delta score, interpreted as the probability of the variant being splice-altering."
            ),
            splice_consequence=hl.struct(
                Description="The consequence term associated with the max delta score in 'splice_ai’."
            ),
            has_duplicate=hl.struct(
                Description="a True/False flag that indicates whether the variant has more than one splice_ai score associated with it*."
            ),
        ),
    ),
    primate_ai=hl.struct(
        sub_annotations=hl.struct(
            primate_ai_score=hl.struct(
                Description="PrimateAI's deleteriousness score from 0 (less deleterious) to 1 (more deleterious)."
            ),
            has_duplicate=hl.struct(
                Description="a True/False flag that indicates whether the variant has more than one primate_ai_score associated with it*."
            ),
        ),
    ),
    AS_lowqual=hl.struct(
        Description="Whether the variant falls below a low quality threshold and was excluded from the gnomAD dataset. We recommend filtering all such variants. This is similar to the GATK LowQual filter, but is allele-specific. GATK computes this annotation at the site level, which uses the least stringent prior for mixed sites."
    ),
    telomere_or_centromere=hl.struct(
        Description="Whether the variant falls within a telomere or centromere region. These variants were excluded from the gnomAD dataset. We recommend filtering all such variants."
    ),
)

SAMPLE_QC_METRICS = [
    "n_deletion",
    "n_het",
    "n_hom_ref",
    "n_hom_var",
    "n_insertion",
    "n_non_ref",
    "n_snp",
    "n_transition",
    "n_transversion",
    "r_het_hom_var",
    "r_insertion_deletion",
    "r_ti_tv",
]


def get_sample_qc_filter_struct_expr(ht):
    """

    :param ht:
    :return:
    """
    logger.info(
        "Read in population specific PCA outliers (list includes one duplicate sample)..."
    )
    hgdp_tgp_pop_outliers_ht = hgdp_tgp_pop_outliers.ht()
    set_to_remove = hgdp_tgp_pop_outliers_ht.s.collect(_localize=False)

    num_outliers = hl.eval(hl.len(set_to_remove))
    num_outliers_found = ht.filter(set_to_remove.contains(ht["s"])).count()
    if hl.eval(hl.len(set_to_remove)) != num_outliers:
        raise ValueError(
            f"Expected {num_outliers} samples to be labeled as population PCA outliers, but found {num_outliers_found}"
        )

    return hl.struct(
        hard_filters=ht.gnomad_sample_filters.hard_filters,
        hard_filtered=ht.gnomad_sample_filters.hard_filtered,
        pop_outlier=set_to_remove.contains(ht["s"]),
    )


def get_relatedness_set_ht(relatedness_ht: hl.Table) -> hl.Table:
    """
    Create Table of all related samples and the relatedness information for all samples they are related to.

    Return Table keyed by sample with a `related_samples` annotation that is a set containing a struct of relatedness
    information for each sample it is related to. Each struct has the following: kin, ibd0, ibd1, and ibd2.

    :param relatedness_ht: Table with inferred relationship information output by pc_relate.
        Keyed by sample pair (i, j).
    :return: Table keyed by sample (s) with all relationship information annotated as a struct.
    """
    relationship_struct = hl.struct(
        kin=relatedness_ht.kin,
        ibd0=relatedness_ht.ibd0,
        ibd1=relatedness_ht.ibd1,
        ibd2=relatedness_ht.ibd2,
    )

    relatedness_ht_i = relatedness_ht.group_by(s=relatedness_ht.i.s).aggregate(
        related_samples=hl.agg.collect_as_set(
            hl.struct(s=relatedness_ht.j.s, **relationship_struct)
        )
    )

    relatedness_ht_j = relatedness_ht.group_by(s=relatedness_ht.j.s).aggregate(
        related_samples=hl.agg.collect_as_set(
            hl.struct(s=relatedness_ht.i.s, **relationship_struct)
        )
    )

    relatedness_ht = relatedness_ht_i.union(relatedness_ht_j)

    return relatedness_ht


def prepare_sample_annotations() -> hl.Table:
    """
    Load meta HT and select row and global annotations for HGDP + TGP subset.

    .. note::

        Expects that `meta.ht()` and `relatedness.ht()` exist. Relatedness pair information will be subset to only
        samples within HGDP + TGP and stored as the `relatedness_inference` annotation of the returned HT.

    :return: Table containing sample metadata for the subset
    """

    logger.info(
        "Subsetting and modifying sample QC metadata to desired globals and annotations"
    )
    meta_ht = meta.ht()
    meta_ht = meta_ht.filter(
        meta_ht.subsets.hgdp | meta_ht.subsets.tgp | (meta_ht.s == SYNDIP)
    )
    meta_ht = meta_ht.select_globals(
        global_annotation_descriptions=GLOBAL_SAMPLE_ANNOTATION_DICT,
        sample_annotation_descriptions=SAMPLE_ANNOTATION_DICT,
        sex_imputation_ploidy_cutoffs=meta_ht.sex_imputation_ploidy_cutoffs,
        population_inference_pca_metrics=hl.struct(
            n_pcs=meta_ht.population_inference_pca_metrics.n_pcs,
            min_prob=meta_ht.population_inference_pca_metrics.min_prob,
        ),
        sample_hard_filter_cutoffs=meta_ht.hard_filter_cutoffs,
        age_distribution=release_sites().ht().index_globals().age_distribution,
    )

    # Use a pre-computed relatedness HT from the Martin group - details of it's creation are
    # here: https://github.com/atgu/hgdp_tgp/blob/master/pca_subcont.ipynb
    relatedness_ht = hgdp_tgp_relatedness.ht()
    subset_samples = meta_ht.s.collect(_localize=False)
    relatedness_ht = relatedness_ht.filter(
        subset_samples.contains(relatedness_ht.i.s)
        & subset_samples.contains(relatedness_ht.j.s)
    )

    relatedness_ht = get_relatedness_set_ht(relatedness_ht)
    meta_ht = meta_ht.select(
        bam_metrics=meta_ht.bam_metrics,
        sample_qc=meta_ht.sample_qc.select(*SAMPLE_QC_METRICS),
        gnomad_sex_imputation=meta_ht.sex_imputation.annotate(
            **meta_ht.sex_imputation.impute_sex_stats
        ).drop("is_female", "impute_sex_stats"),
        gnomad_population_inference=meta_ht.population_inference.drop(
            "training_pop", "training_pop_all"
        ),
        gnomad_sample_qc_residuals=meta_ht.sample_qc.select(
            *[k for k in meta_ht.sample_qc.keys() if "_residual" in k]
        ),
        gnomad_sample_filters=meta_ht.sample_filters.select(
            "hard_filters", "hard_filtered", "release_related", "qc_metrics_filters"
        ),
        gnomad_high_quality=meta_ht.high_quality,
        gnomad_release=meta_ht.release,
        relatedness_inference=hl.struct(
            related_samples=hl.coalesce(
                relatedness_ht[meta_ht.key].related_samples,
                hl.empty_set(
                    hl.dtype(
                        "struct{s: str, kin: float64, ibd0: float64, ibd1: float64, ibd2: float64}"
                    )
                ),
            ),
            related=hl.is_defined(hgdp_tgp_related_samples_to_drop.ht()[meta_ht.key]),
        ),
        gnomad_labeled_subpop=meta_ht.project_meta.project_subpop,
    )

    logger.info("Loading additional sample metadata from Martin group...")
    hgdp_tgp_meta_ht = hgdp_tgp_meta.ht()
    hgdp_tgp_meta_ht = hgdp_tgp_meta_ht.select(
        project=hgdp_tgp_meta_ht.hgdp_tgp_meta.Project,
        study_region=hgdp_tgp_meta_ht.hgdp_tgp_meta.Study.region,
        population=hgdp_tgp_meta_ht.hgdp_tgp_meta.Population,
        geographic_region=hgdp_tgp_meta_ht.hgdp_tgp_meta.Genetic.region,
        latitude=hgdp_tgp_meta_ht.hgdp_tgp_meta.Latitude,
        longitude=hgdp_tgp_meta_ht.hgdp_tgp_meta.Longitude,
        hgdp_technical_meta=hgdp_tgp_meta_ht.bergstrom.select("source", "library_type"),
    )
    hgdp_tgp_meta_ht = hgdp_tgp_meta_ht.union(
        hl.Table.parallelize(
            [hl.struct(s=SYNDIP, project="synthetic_diploid_truth_sample")]
        ).key_by("s"),
        unify=True,
    )

    logger.info(
        "Removing 'v3.1::' from the sample names, these were added because there are duplicates of some 1KG samples"
        " in the full gnomAD dataset..."
    )
    meta_ht = meta_ht.key_by(s=meta_ht.s.replace("v3.1::", ""))

    logger.info("Adding sample QC struct and sample metadata from Martin group...")
    meta_ht = meta_ht.annotate(sample_filters=get_sample_qc_filter_struct_expr(meta_ht))
    hgdp_tgp_pcs_indexed = hgdp_tgp_pcs.ht()[meta_ht.key]

    meta_ht = meta_ht.transmute(
        hgdp_tgp_meta=hl.struct(
            **hgdp_tgp_meta_ht[meta_ht.key],
            global_pca_scores=hgdp_tgp_pcs_indexed.pca_preoutlier_global_scores,
            subcontinental_pca=hl.struct(
                pca_scores=hgdp_tgp_pcs_indexed.pca_scores,
                pca_scores_outliers_removed=hgdp_tgp_pcs_indexed.pca_scores_outliers_removed,
                outlier=meta_ht.sample_filters.pop_outlier,
            ),
            gnomad_labeled_subpop=meta_ht.gnomad_labeled_subpop,
        ),
        high_quality=~meta_ht.sample_filters.hard_filtered
        & ~meta_ht.sample_filters.pop_outlier,
    )
    meta_ht.show(50)

    return meta_ht


# TODO: Might be good to generalize this because a similar function is used in creating the release sites HT.
def prepare_variant_annotations(ht: hl.Table, filter_lowqual: bool = True) -> hl.Table:
    """
    Load and join all Tables with variant annotations.

    :param ht: Input HT to add variant annotations to.
    :param filter_lowqual: If True, filter out lowqual variants using the info HT's AS_lowqual.
    :return: Table containing joined annotations.
    """
    logger.info("Loading annotation tables...")
    filters_ht = final_filter.ht()
    vep_ht = vep.ht()
    dbsnp_ht = dbsnp.ht().select("rsid")
    info_ht = get_info().ht()
    analyst_ht = analyst_annotations.ht()
    freq_ht = get_freq().ht()
    score_name = hl.eval(filters_ht.filtering_model.score_name)
    subset_freq = get_freq(subset="hgdp-tgp").ht()
    release_ht = release_sites().ht()

    if filter_lowqual:
        logger.info("Filtering lowqual variants...")
        ht = ht.filter(info_ht[ht.key].AS_lowqual, keep=False)

    logger.info("Assembling 'info' field...")
    info_fields = SITE_FIELDS + AS_FIELDS
    info_fields.remove("AS_VQSLOD")
    missing_info_fields = set(info_fields).difference(info_ht.info.keys())
    select_info_fields = set(info_fields).intersection(info_ht.info.keys())
    logger.info(
        "The following fields are not found in the info HT: %s", missing_info_fields,
    )

    # NOTE: SOR and AS_SOR annotations are now added to the info HT by default with get_as_info_expr and
    # get_site_info_expr in gnomad_methods, but they were not for v3 or v3.1
    keyed_filters = filters_ht[info_ht.key]
    info_ht = info_ht.transmute(
        info=info_ht.info.select(
            *select_info_fields,
            AS_SOR=keyed_filters.AS_SOR,
            SOR=keyed_filters.SOR,
            singleton=keyed_filters.singleton,
            transmitted_singleton=keyed_filters.transmitted_singleton,
            omni=keyed_filters.omni,
            mills=keyed_filters.mills,
            monoallelic=keyed_filters.monoallelic,
            InbreedingCoeff=freq_ht[info_ht.key].InbreedingCoeff,
            **{f"{score_name}": keyed_filters[f"{score_name}"]},
        )
    )

    logger.info(
        "Preparing gnomad freq information from the release HT, remove downsampling and subset info from freq, "
        "freq_meta, and freq_index_dict"
    )
    full_release_freq_meta = release_ht.freq_meta.collect()[0]
    freq_meta = [
        x
        for x in full_release_freq_meta
        if "downsampling" not in x and "subset" not in x
    ]
    index_keep = [
        i
        for i, x in enumerate(full_release_freq_meta)
        if "downsampling" not in x and "subset" not in x
    ]
    freq_index_dict = release_ht.freq_index_dict.collect()[0]
    freq_index_dict = {k: v for k, v in freq_index_dict.items() if v in index_keep}

    logger.info("Assembling all variant annotations...")
    filters_ht = filters_ht.annotate(
        allele_info=hl.struct(
            variant_type=filters_ht.variant_type,
            allele_type=filters_ht.allele_type,
            n_alt_alleles=filters_ht.n_alt_alleles,
            was_mixed=filters_ht.was_mixed,
        ),
    )

    keyed_filters = filters_ht[ht.key]
    keyed_release = release_ht[ht.key]
    keyed_info = info_ht[ht.key]
    ht = ht.annotate(
        a_index=keyed_info.a_index,
        was_split=keyed_info.was_split,
        rsid=dbsnp_ht[ht.key].rsid,
        filters=keyed_filters.filters,
        info=keyed_info.info,
        vep=vep_ht[ht.key].vep.drop("colocated_variants"),
        vqsr=keyed_filters.vqsr,
        region_flag=region_flag_expr(
            ht,
            non_par=False,
            prob_regions={"lcr": lcr_intervals.ht(), "segdup": seg_dup_intervals.ht()},
        ),
        allele_info=keyed_filters.allele_info,
        **analyst_ht[ht.key],
        hgdp_tgp_freq=subset_freq[ht.key].freq,
        gnomad_freq=keyed_release.freq[: len(freq_meta)],
        gnomad_popmax=keyed_release.popmax,
        gnomad_faf=keyed_release.faf,
        gnomad_raw_qual_hists=keyed_release.raw_qual_hists,
        gnomad_qual_hists=keyed_release.qual_hists,
        gnomad_age_hist_het=keyed_release.age_hist_het,
        gnomad_age_hist_hom=keyed_release.age_hist_hom,
        AS_lowqual=keyed_info.AS_lowqual,
        telomere_or_centromere=hl.is_defined(telomeres_and_centromeres.ht()[ht.locus]),
    )

    logger.info("Adding global variant annotations...")
    ht = ht.annotate_globals(
        global_annotation_descriptions=GLOBAL_VARIANT_ANNOTATION_DICT,
        variant_annotation_descriptions=VARIANT_ANNOTATION_DICT,
        hgdp_tgp_freq_meta=subset_freq.index_globals().freq_meta,
        hgdp_tgp_freq_index_dict=make_freq_index_dict(
            hl.eval(subset_freq.index_globals().freq_meta),
            pops=POPS_STORED_AS_SUBPOPS,
            subsets=["hgdp|tgp"],
            label_delimiter="-",
        ),
        gnomad_freq_meta=freq_meta,
        gnomad_freq_index_dict=freq_index_dict,
        gnomad_faf_index_dict=release_ht.index_globals().faf_index_dict,
        gnomad_faf_meta=release_ht.index_globals().faf_meta,
        vep_version=release_ht.index_globals().vep_version,
        vep_csq_header=release_ht.index_globals().vep_csq_header,
        dbsnp_version=release_ht.index_globals().dbsnp_version,
        variant_filtering_model=release_ht.index_globals().filtering_model.drop(
            "model_id"
        ),
        variant_inbreeding_coeff_cutoff=filters_ht.index_globals().inbreeding_coeff_cutoff,
    )

    return ht


def adjust_subset_alleles(mt: hl.MatrixTable) -> hl.MatrixTable:
    """
    Modeled after Hail's `filter_alleles` module to adjust the allele annotation to include only alleles present in the MT.

    .. note::

        Should be used only on sparse Matrix Tables

    Uses `hl.agg.any` to determine if an allele if found in the MT. The alleles annotations will only include reference
    alleles and alternate alleles that are in MT. `mt.LA` will be adjusted to the new alleles annotation.

    :param mt: MatrixTable to subset locus alleles
    :return: MatrixTable with alleles adjusted to only those with a sample containing a non reference allele
    """
    mt = mt.annotate_rows(
        _keep_allele=hl.agg.array_agg(
            lambda i: hl.agg.any(mt.LA.contains(i[0])), hl.enumerate(mt.alleles)
        )
    )
    new_to_old = (
        hl.enumerate(mt._keep_allele).filter(lambda elt: elt[1]).map(lambda elt: elt[0])
    )
    old_to_new_dict = hl.dict(
        hl.enumerate(
            hl.enumerate(mt.alleles).filter(lambda elt: mt._keep_allele[elt[0]])
        ).map(lambda elt: (elt[1][1], elt[0]))
    )
    mt = mt.annotate_rows(
        _old_to_new=hl.bind(
            lambda d: mt.alleles.map(lambda a: d.get(a)), old_to_new_dict
        ),
        _new_to_old=new_to_old,
    )
    new_locus_alleles = hl.min_rep(
        mt.locus, mt._new_to_old.map(lambda i: mt.alleles[i])
    )
    mt = mt.key_rows_by(
        locus=new_locus_alleles.locus, alleles=new_locus_alleles.alleles
    )
    mt = mt.annotate_entries(LA=mt.LA.map(lambda x: mt._old_to_new[x]))

    return mt.drop("_keep_allele", "_new_to_old", "_old_to_new")


def create_full_subset_dense_mt(
    mt: hl.MatrixTable, meta_ht: hl.Table, variant_annotation_ht: hl.Table
):
    """
    Create the subset dense release MatrixTable with multi-allelic variants and all sample and variant annotations.

    .. note::

        This function uses the sparse subset MT and filters out LowQual variants and centromeres and telomeres.

    :param mt: Sparse subset release MatrixTable
    :param meta_ht: Metadata HT to use for sample (column) annotations
    :param variant_annotation_ht: Metadata HT to use for variant (row) annotations
    :return: Dense release MatrixTable with all row, column, and global annotations
    """
    logger.info(
        "Adding subset's sample QC metadata to MT columns and global annotations to MT globals..."
    )
    mt = mt.annotate_cols(**meta_ht[mt.col_key])
    mt = mt.annotate_globals(
        global_annotation_descriptions=hl.literal(GLOBAL_ANNOTATION_DICT),
        **meta_ht.drop("global_annotation_descriptions").index_globals(),
    )

    logger.info(
        "Annotate entries with het non ref status for use in the homozygous alternate depletion fix..."
    )
    mt = mt.annotate_entries(_het_non_ref=mt.LGT.is_het_non_ref())

    logger.info("Splitting multi-allelics...")
    mt = hl.experimental.sparse_split_multi(mt, filter_changed_loci=True)

    logger.info("Computing adj and sex adjusted genotypes...")
    mt = mt.annotate_entries(
        GT=adjusted_sex_ploidy_expr(
            mt.locus, mt.GT, mt.gnomad_sex_imputation.sex_karyotype
        ),
        adj=get_adj_expr(mt.GT, mt.GQ, mt.DP, mt.AD),
    )

    logger.info(
        "Setting het genotypes at sites with > 1% AF (using precomputed v3.0 frequencies) and > 0.9 AB to homalt..."
    )
    # NOTE: Using v3.0 frequencies here and not v3.1 frequencies because
    # the frequency code adjusted genotypes (homalt depletion fix) using v3.0 frequencies
    # https://github.com/broadinstitute/gnomad_qc/blob/efea6851a421f4bc66b73db588c0eeeb7cd27539/gnomad_qc/v3/annotations/generate_freq_data_hgdp_tgp.py#L129
    freq_ht = get_freq(version="3").ht()
    mt = hom_alt_depletion_fix(
        mt, het_non_ref_expr=mt._het_non_ref, af_expr=freq_ht[mt.row_key].freq[0].AF
    )
    mt = mt.drop("_het_non_ref")

    logger.info("Add all variant annotations and variant global annotations...")
    mt = mt.annotate_rows(**variant_annotation_ht[mt.row_key])
    mt = mt.annotate_globals(**variant_annotation_ht.index_globals())

    logger.info("Densify MT...")
    mt = hl.experimental.densify(mt)

    logger.info(
        "Filter out LowQual variants (using allele-specific annotation) and variants within centromere and telomere "
        "regions..."
    )
    mt = mt.filter_rows(
        ~mt.AS_lowqual & ~mt.telomere_or_centromere & (hl.len(mt.alleles) > 1)
    )
    mt = mt.drop("AS_lowqual", "telomere_or_centromere")

    return mt


def main(args):
    hl.init(log="/hgdp_1kg_subset.log", default_reference="GRCh38")

    test = args.test
    sample_annotation_resource = hgdp_1kg_subset_annotations(test=test)
    variant_annotation_resource = hgdp_1kg_subset_annotations(sample=False, test=test)
    sparse_mt_resource = hgdp_1kg_subset(test=test)
    dense_mt_resource = hgdp_1kg_subset(dense=True, test=test)

    if args.create_sample_annotation_ht:
        meta_ht = prepare_sample_annotations()
        meta_ht.write(sample_annotation_resource.path, overwrite=args.overwrite)

    if (
        test
        and (
            args.export_sample_annotation_tsv
            or args.create_subset_sparse_mt
            or args.create_subset_dense_mt
        )
        and not file_exists(sample_annotation_resource.path)
    ):
        raise DataException(
            "There is currently no sample meta HT for the HGDP + TGP subset written to temp for testing. "
            "Run '--create_sample_meta' with '--test' to create one."
        )

    if args.export_sample_annotation_tsv:
        meta_ht = sample_annotation_resource.ht()
        meta_ht.export(hgdp_1kg_subset_sample_tsv(test=test))

    if args.create_subset_sparse_mt:
        # NOTE: no longer filtering to high_quality by request from Alicia Martin, but we do filter to variants in
        # high_quality samples in the frequency code, so how to handle that, just filter to martin high_quality? Can we maybe justt apply the frequency code to tthe dense dataaset after that filter instead?
        meta_ht = sample_annotation_resource.ht()
        mt = get_gnomad_v3_mt(
            key_by_locus_and_alleles=True, remove_hard_filtered_samples=False
        )
        if test:
            logger.info(
                "Filtering MT to first %d partitions for testing",
                args.test_n_partitions,
            )
            mt = mt._filter_partitions(range(args.test_n_partitions))

        logger.info(
            "Filtering MT columns to HGDP + TGP samples and the CHMI haploid sample (syndip)"
        )
        keyed_full_meta = meta.ht()[mt.col_key]
        mt = mt.filter_cols(
            keyed_full_meta.subsets.hgdp
            | keyed_full_meta.subsets.tgp
            | (mt.s == SYNDIP)
        )

        logger.info(
            "Removing 'v3.1::' from the column names, these were added because there are duplicates of some 1KG samples"
            " in the full gnomAD dataset..."
        )
        mt = mt.key_cols_by(s=mt.s.replace("v3.1::", ""))

        # Adjust alleles and LA to include only alleles present in the subset
        mt = adjust_subset_alleles(mt)

        logger.info(
            "Note: for the finalized HGDP + TGP subset frequency, dense MT, and VCFs we adjust the sex genotypes and add "
            "a fix for older GATK gVCFs with a known depletion of homozygous alternate alleles, and remove standard "
            "GATK LowQual variants and variants in centromeres and telomeres."
        )

        mt.write(sparse_mt_resource.path, overwrite=args.overwrite)

    if (
        test
        and (args.create_variant_annotation_ht or args.create_subset_dense_mt)
        and not file_exists(sparse_mt_resource.path)
    ):
        raise DataException(
            "There is currently no sparse test MT for the HGDP + TGP subset. Run '--create_subset_sparse_mt' "
            "with '--test' to create one."
        )

    if args.create_variant_annotation_ht:
        logger.info("Creating variant annotation Hail Table")
        ht = sparse_mt_resource.mt().rows().select().select_globals()

        logger.info("Splitting multi-allelics and filtering out ref block variants...")
        ht = hl.split_multi(ht)
        ht = ht.filter(hl.len(ht.alleles) > 1)

        ht = prepare_variant_annotations(ht, filter_lowqual=False)
        ht.write(variant_annotation_resource.path, overwrite=args.overwrite)

    if args.create_subset_dense_mt:
        meta_ht = sample_annotation_resource.ht()
        if test and not file_exists(variant_annotation_resource.path):
            raise DataException(
                "There is currently no variant annotation HT for the HGDP + TGP subset written to temp for testing. "
                "Run '--create_variant_annotation_ht' with '--test' to create one."
            )
        variant_annotation_ht = variant_annotation_resource.ht()

        mt = sparse_mt_resource.mt()
        mt = mt.select_entries(*SPARSE_ENTRIES)
        mt = create_full_subset_dense_mt(mt, meta_ht, variant_annotation_ht)

        logger.info(
            "Writing dense HGDP + TGP MT with all sample and variant annotations"
        )
        mt.write(dense_mt_resource.path, overwrite=args.overwrite)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="This script subsets the gnomAD v3.1 release to only HGDP and 1KG samples."
    )
    parser.add_argument(
        "--create_sample_annotation_ht",
        help="Create the HGDP + 1KG subset sample metadata Hail Table.",
        action="store_true",
    )
    parser.add_argument(
        "--export_sample_annotation_tsv",
        help="Pull sample subset metadata and export to a .tsv.",
        action="store_true",
    )
    parser.add_argument(
        "--create_subset_sparse_mt",
        help="Create the HGDP + 1KG subset sparse MT.",
        action="store_true",
    )
    parser.add_argument(
        "--create_variant_annotation_ht",
        help="Create the HGDP + 1KG subset variant annotation Hail Table.",
        action="store_true",
    )
    parser.add_argument(
        "--create_subset_dense_mt",
        help="Create the HGDP + 1KG subset dense MT.",
        action="store_true",
    )
    parser.add_argument(
        "--test",
        help=(
            "Run small test export on a subset of partitions of the MT. Writes to temp rather than writing to the "
            "main bucket."
        ),
        action="store_true",
    )
    parser.add_argument(
        "--test_n_partitions",
        default=5,
        type=int,
        help="Number of partitions to use for testing.",
    )
    parser.add_argument(
        "-o",
        "--overwrite",
        help="Overwrite all data from this subset (default: False)",
        action="store_true",
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
