"""Script containing sample QC related resources."""
import hail as hl
from gnomad.resources.resource_utils import (
    MatrixTableResource,
    TableResource,
    VersionedMatrixTableResource,
    VersionedTableResource,
)

from gnomad_qc.v4.resources.basics import get_checkpoint_path, qc_temp_prefix
from gnomad_qc.v4.resources.constants import CURRENT_VERSION, VERSIONS


def get_sample_qc_root(
    version: str = CURRENT_VERSION, test: bool = False, data_type="exomes"
) -> str:
    """
    Return path to sample QC root folder.

    :param version: Version of sample QC path to return
    :param test: Whether to use a tmp path for analysis of the test VDS instead of the full v4 VDS
    :param data_type: Data type used in sample QC, e.g. "exomes" or "joint"
    :return: Root to sample QC path
    """
    return (
        f"gs://gnomad-tmp/gnomad_v{version}_testing/sample_qc/{data_type}"
        if test
        else f"gs://gnomad/v{version}/sample_qc/{data_type}"
    )


######################################################################
# Hard-filtering resources
######################################################################


def get_sample_qc(
    strat: str = "all", test: bool = False, data_type: str = "exomes"
) -> VersionedTableResource:
    """
    Get sample QC annotations generated by Hail for the specified stratification.

    Possible values for `strat`:
        - bi_allelic
        - multi_allelic
        - all

    :param strat: Which stratification to return
    :param test: Whether to use a tmp path for analysis of the test VDS instead of the full v4 VDS
    :param data_type: Data type used in sample QC, e.g. "exomes" or "joint"
    :return: Sample QC table
    """
    return VersionedTableResource(
        CURRENT_VERSION,
        {
            version: TableResource(
                f"{get_sample_qc_root(version, test, data_type)}/hard_filtering/gnomad.{data_type}.v{version}.sample_qc_all_{'' if strat == 'all' else strat}.ht"
            )
            for version in VERSIONS
        },
    )


# v4 samples that failed fingerprinting.
fingerprinting_failed = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/hard_filtering/gnomad.exomes.v{version}.fingerprintcheck_failures.ht"
        )
        for version in VERSIONS
    },
)

# Mean chr20 DP per sample using Hail's interval_coverage results.
sample_chr20_mean_dp = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/hard_filtering/gnomad.exomes.v{version}.sample_chr20_mean_dp.ht"
        )
        for version in VERSIONS
    },
)

# Sample contamination estimate Table.
contamination = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/hard_filtering/gnomad.exomes.v{version}.contamination.ht"
        )
        for version in VERSIONS
    },
)

hard_filtered_samples_no_sex = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/hard_filtering/gnomad.exomes.v{version}.hard_filtered_samples_no_sex.ht"
        )
        for version in VERSIONS
    },
)

hard_filtered_samples = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/hard_filtering/gnomad.exomes.v{version}.hard_filtered_samples.ht"
        )
        for version in VERSIONS
    },
)

######################################################################
# Platform inference resources
######################################################################

# VDS Hail interval_coverage results.
interval_coverage = VersionedMatrixTableResource(
    CURRENT_VERSION,
    {
        version: MatrixTableResource(
            f"{get_sample_qc_root(version)}/platform_inference/gnomad.exomes.v{version}.interval_coverage.mt"
        )
        for version in VERSIONS
    },
)


def _get_platform_pca_ht_path(part: str, version: str = CURRENT_VERSION) -> str:
    """
    Get path to files related to platform PCA.

    :param part: String indicating the type of PCA file to return (loadings, eigenvalues, or scores)
    :param version: Version of sample QC path to return
    :return: Path to requested platform PCA file
    """
    return f"{get_sample_qc_root(version)}/platform_inference/gnomad.exomes.v{version}.platform_pca_{part}.ht"


platform_pca_loadings = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            _get_platform_pca_ht_path(
                "loadings",
                version,
            )
        )
        for version in VERSIONS
    },
)

platform_pca_scores = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            _get_platform_pca_ht_path(
                "scores",
                version,
            )
        )
        for version in VERSIONS
    },
)

platform_pca_eigenvalues = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            _get_platform_pca_ht_path(
                "eigenvalues",
                version,
            )
        )
        for version in VERSIONS
    },
)

# Inferred sample platforms.
platform = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/platform_inference/gnomad.exomes.v{version}.platform.ht"
        )
        for version in VERSIONS
    },
)

######################################################################
# Sex inference resources
######################################################################

# HT with bi-allelic SNPs on chromosome X used in sex imputation f-stat calculation.
f_stat_sites = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/sex_inference/gnomad.exomes.v{version}.f_stat_sites.ht"
        )
        for version in VERSIONS
    },
)

# Sex chromosome coverage aggregate stats MT.
sex_chr_coverage = VersionedMatrixTableResource(
    CURRENT_VERSION,
    {
        version: MatrixTableResource(
            f"{get_sample_qc_root(version)}/sex_inference/gnomad.exomes.v{version}.sex_chr_coverage.mt"
        )
        for version in VERSIONS
    },
)

# Table containing aggregate stats for interval QC specific to sex imputation.
sex_imputation_interval_qc = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/sex_inference/gnomad.exomes.v{version}.sex_imputation_interval_qc.ht"
        )
        for version in VERSIONS
    },
)

# Ploidy imputation results.
ploidy = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/sex_inference/gnomad.exomes.v{version}.ploidy.ht"
        )
        for version in VERSIONS
    },
)


# Sex imputation results.
def get_ploidy_cutoff_json_path(version: str = CURRENT_VERSION, test: bool = False):
    """
    Get the sex karyotype ploidy cutoff JSON path for the indicated gnomAD version.

    :param version: Version of the JSON to return.
    :param test: Whether to use a tmp path for a test JSON.
    :return: Path of sex karyotype ploidy cutoff JSON.
    """
    if test:
        return f"{qc_temp_prefix(version)}ploidy_cutoffs.test.json"
    else:
        return f"{get_sample_qc_root(version)}/sex_inference/gnomad.exomes.v{version}.ploidy_cutoffs.json"


sex = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/sex_inference/gnomad.exomes.v{version}.sex.ht"
        )
        for version in VERSIONS
    },
)

######################################################################
# Interval QC resources
######################################################################

# Table containing aggregate stats for interval QC.
interval_qc = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/interval_qc/gnomad.exomes.v{version}.interval_qc.ht"
        )
        for version in VERSIONS
    },
)

# Table with interval QC pass annotation.
interval_qc_pass = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/interval_qc/gnomad.exomes.v{version}.interval_qc_pass.ht"
        )
        for version in VERSIONS
    },
)


######################################################################
# Generate QC MT resources
######################################################################


def get_predetermined_qc(
    version: str = CURRENT_VERSION, test: bool = False
) -> VersionedMatrixTableResource:
    """
    Get the dense MatrixTableResource of all predetermined QC sites for the indicated gnomAD version.

    :param version: Version of QC MatrixTableResource to return.
    :param test: Whether to use a tmp path for a test MatrixTableResource.
    :return: MatrixTableResource of predetermined QC sites.
    """
    if test:
        return MatrixTableResource(
            get_checkpoint_path(f"dense_pre_ld_prune_qc_sites.v{version}.test", mt=True)
        )
    elif version == "3.1":
        return v3_predetermined_qc
    else:
        return v4_predetermined_qc.versions[version]


# HT of pre LD pruned variants chosen from CCDG, gnomAD v3, and UKB variant info.
# See: https://github.com/Nealelab/ccdg_qc/blob/master/scripts/pca_variant_filter.py
predetermined_qc_sites = TableResource(
    "gs://gnomad/v4.0/sample_qc/pre_ld_pruning_qc_variants.ht"
)

# gnomAD v3 dense MT of all predetermined possible QC sites `predetermined_qc_sites`.
v3_predetermined_qc = MatrixTableResource(
    "gs://gnomad/sample_qc/mt/genomes_v3.1/gnomad.genomes.v3.1.pre_ld_prune_qc_sites.dense.mt"
)

# gnomAD v4 dense MT of all predetermined possible QC sites `predetermined_qc_sites`.
v4_predetermined_qc = VersionedMatrixTableResource(
    CURRENT_VERSION,
    {
        version: MatrixTableResource(
            f"{get_sample_qc_root(version)}/qc_mt/gnomad.exomes.v{version}.pre_ld_prune_qc_sites.dense.mt"
        )
        for version in VERSIONS
    },
)


def get_joint_qc(test: bool = False) -> VersionedMatrixTableResource:
    """
    Get the dense MatrixTableResource at final joint v3 and v4 QC sites.

    :param test: Whether to use a tmp path for a test resource.
    :return: MatrixTableResource of QC sites.
    """
    return VersionedMatrixTableResource(
        CURRENT_VERSION,
        {
            version: MatrixTableResource(
                f"{get_sample_qc_root(version, test)}/qc_mt/gnomad.joint.v{version}.qc.mt"
            )
            for version in VERSIONS
        },
    )


# v3 and v4 combined sample metadata Table for relatedness and population inference.
joint_qc_meta = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/qc_mt/gnomad.joint.v{version}.qc_meta.ht"
        )
        for version in VERSIONS
    },
)


######################################################################
# Relatedness resources
######################################################################


def get_cuking_input_path(version: str = CURRENT_VERSION, test: bool = False) -> str:
    """
    Return the path containing the input files read by cuKING.

    Those files correspond to Parquet tables derived from the dense QC matrix.

    :param version: gnomAD version.
    :param test: Whether to return a path corresponding to a test subset.
    :return: Temporary path to hold Parquet input tables for running cuKING.
    """
    # cuKING inputs can be easily regenerated, so use a temp location.
    return f"{qc_temp_prefix(version)}cuking_input{'_test' if test else ''}.parquet"


def get_cuking_output_path(version: str = CURRENT_VERSION, test: bool = False) -> str:
    """
    Return the path containing the output files written by cuKING.

    Those files correspond to Parquet tables containing relatedness results.

    :param version: gnomAD version.
    :param test: Whether to return a path corresponding to a test subset.
    :return: Temporary path to hold Parquet output tables for running cuKING.
    """
    # cuKING outputs can be easily regenerated, so use a temp location.
    return f"{qc_temp_prefix(version)}cuking_output{'_test' if test else ''}.parquet"


pc_relate_pca_scores = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/relatedness/gnomad.exomes.v{version}.pc_scores.ht"
        )
        for version in VERSIONS
    },
)


def relatedness(method: str = "cuking", test: bool = False):
    """
    Get the VersionedTableResource for relatedness results.

    :param method: Method of relatedness inference to return VersionedTableResource for.
        One of 'cuking' or 'pc_relate'.
    :param test: Whether to use a tmp path for a test resource.
    :return: VersionedTableResource.
    """
    if method not in {"cuking", "pc_relate"}:
        raise ValueError("method must be one of 'cuking' or 'pc_relate'!")

    return VersionedTableResource(
        CURRENT_VERSION,
        {
            version: TableResource(
                f"{get_sample_qc_root(version, test)}/relatedness/gnomad.exomes.v{version}.relatedness.{method}.ht"
            )
            for version in VERSIONS
        },
    )


def ibd(test: bool = False) -> VersionedTableResource:
    """
    Get VersionedTableResource for identity-by-descent (ibd) on cuKING related pairs.

    :param test: Whether to use a tmp path for a test resource.
    :return: VersionedTableResource.
    """
    return VersionedTableResource(
        CURRENT_VERSION,
        {
            version: TableResource(
                f"{get_sample_qc_root(version, test)}/relatedness/gnomad.exomes.v{version}.ibd.ht"
            )
            for version in VERSIONS
        },
    )


def _import_related_samples_to_drop(**kwargs):
    ht = hl.import_table(**kwargs)
    ht = ht.key_by(s=ht.f0)

    return ht


def pca_related_samples_to_drop(test: bool = False) -> VersionedTableResource:
    """
    Get the VersionedTableResource for samples to drop for PCA due to them being related.

    :param test: Whether to use a tmp path for a test resource.
    :return: VersionedTableResource.
    """
    return VersionedTableResource(
        CURRENT_VERSION,
        {
            version: TableResource(
                f"{get_sample_qc_root(version, test)}/relatedness/gnomad.exomes.v{version}.related_samples_to_drop_for_pca.ht"
            )
            for version in VERSIONS
        },
    )


# Ranking of all samples based on quality metrics. Used to remove relateds for PCA.
pca_samples_rankings = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/relatedness/gnomad.exomes.v{version}.pca_samples_ranking.ht"
        )
        for version in VERSIONS
    },
)

# Ranking of all release samples based on quality metrics. Used to remove relateds for
# release.
release_samples_rankings = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/relatedness/gnomad.exomes.v{version}.release_samples_ranking.ht"
        )
        for version in VERSIONS
    },
)

# Duplicated (or twin) samples.
duplicates = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/relatedness/gnomad.exomes.v{version}.duplicates.ht"
        )
        for version in VERSIONS
    },
)


# Related samples to drop for release
release_related_samples_to_drop = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/relatedness/gnomad.exomes.v{version}.related_release_samples_to_drop.ht"
        )
        for version in VERSIONS
    },
)

######################################################################
# Ancestry inference resources
######################################################################


def _get_ancestry_pca_ht_path(
    part: str,
    version: str = CURRENT_VERSION,
    include_unreleasable_samples: bool = False,
    test: bool = False,
    data_type: str = "joint",
) -> str:
    """
    Get path to files related to ancestry PCA.

    :param part: String indicating the type of PCA file to return (loadings, eigenvalues, or scores).
    :param version: Version of sample QC path to return.
    :param include_unreleasable_samples: Whether the PCA included unreleasable samples.
    :param data_type: Data type used in sample QC, e.g. "exomes" or "joint"
    :return: Path to requested ancestry PCA file.
    """
    return (
        f"{get_sample_qc_root(version,test,data_type)}/gnomad.{data_type}.v{version}.pca_{part}{'_with_unreleasable_samples' if include_unreleasable_samples else ''}.ht"
    )


def ancestry_pca_loadings(
    include_unreleasable_samples: bool = False,
    test: bool = False,
    data_type: str = "joint",
) -> VersionedTableResource:
    """
    Get the ancestry PCA loadings VersionedTableResource.

    :param include_unreleasable_samples: Whether to get the PCA loadings from the PCA that used unreleasable samples.
    :param test: Whether to use a temp path.
    :param data_type: Data type used in sample QC, e.g. "exomes" or "joint"
    :return: Ancestry PCA loadings
    """
    return VersionedTableResource(
        CURRENT_VERSION,
        {
            version: TableResource(
                _get_ancestry_pca_ht_path(
                    "loadings", version, include_unreleasable_samples, test, data_type
                )
            )
            for version in VERSIONS
        },
    )


def ancestry_pca_scores(
    include_unreleasable_samples: bool = False,
    test: bool = False,
    data_type: str = "joint",
) -> VersionedTableResource:
    """
    Get the ancestry PCA scores VersionedTableResource.

    :param include_unreleasable_samples: Whether to get the PCA scores from the PCA that used unreleasable samples.
    :param test: Whether to use a temp path.
    :param data_type: Data type used in sample QC, e.g. "exomes" or "joint"
    :return: Ancestry PCA scores
    """
    return VersionedTableResource(
        CURRENT_VERSION,
        {
            version: TableResource(
                _get_ancestry_pca_ht_path(
                    "scores", version, include_unreleasable_samples, test, data_type
                )
            )
            for version in VERSIONS
        },
    )


def ancestry_pca_eigenvalues(
    include_unreleasable_samples: bool = False,
    test: bool = False,
    data_type: str = "joint",
) -> VersionedTableResource:
    """
    Get the ancestry PCA eigenvalues VersionedTableResource.

    :param include_unreleasable_samples: Whether to get the PCA eigenvalues from the PCA that used unreleasable samples.
    :param test: Whether to use a temp path.
    :param data_type: Data type used in sample QC, e.g. "exomes" or "joint"
    :return: Ancestry PCA eigenvalues
    """
    return VersionedTableResource(
        CURRENT_VERSION,
        {
            version: TableResource(
                _get_ancestry_pca_ht_path(
                    "eigenvalues",
                    version,
                    include_unreleasable_samples,
                    test,
                    data_type,
                )
            )
            for version in VERSIONS
        },
    )


def pop_tsv_path(
    version: str = CURRENT_VERSION,
    test: bool = False,
    data_type: str = "joint",
    only_train_on_hgdp_tgp: bool = False,
) -> str:
    """
    Path to tab delimited file indicating inferred sample populations.

    :param version: gnomAD Version
    :param test: Whether the RF assignment used a test dataset.
    :param data_type: Data type used in sample QC, e.g. "exomes" or "joint".
    :param only_train_on_hgdp_tgp: Whether the RF classifier trained using only the HGDP and 1KG populations. Default is False.
    :return: String path to sample populations
    """
    return (
        f"{get_sample_qc_root(version,test,data_type)}/ancestry_inference/gnomad.{data_type}.v{version}.{'hgdp_tgp_training.' if only_train_on_hgdp_tgp else ''}RF_pop_assignments.txt.gz"
    )


def pop_rf_path(
    version: str = CURRENT_VERSION,
    test: bool = False,
    data_type: str = "joint",
    only_train_on_hgdp_tgp: bool = False,
) -> str:
    """
    Path to RF model used for inferring sample populations.

    :param version: gnomAD Version
    :param test: Whether the RF assignment was from a test dataset.
    :param data_type: Data type used in sample QC, e.g. "exomes" or "joint".
    :param only_train_on_hgdp_tgp: Whether the RF classifier trained using only the HGDP and 1KG populations. Default is False.
    :return: String path to sample pop RF model
    """
    return (
        f"{get_sample_qc_root(version,test, data_type)}/ancestry_inference/gnomad.{data_type}.v{version}.{'hgdp_tgp_training.' if only_train_on_hgdp_tgp else ''}pop.RF_fit.pickle"
    )


def get_pop_ht(
    version: str = CURRENT_VERSION,
    test: bool = False,
    data_type: str = "joint",
    only_train_on_hgdp_tgp: bool = False,
):
    """
    Get the TableResource of samples' inferred population for the indicated gnomAD version.

    :param version: Version of pop TableResource to return.
    :param test: Whether to use the test version of the pop TableResource.
    :param data_type: Data type used in sample QC, e.g. "exomes" or "joint".
    :param only_train_on_hgdp_tgp: Whether the RF classifier trained using only the HGDP and 1KG populations. Default is False.
    :return: TableResource of sample pops.
    """
    return TableResource(
        f"{get_sample_qc_root(version,test, data_type)}/ancestry_inference/gnomad.{data_type}.v{version}.{'hgdp_tgp_training.' if only_train_on_hgdp_tgp else ''}pop.ht"
    )


######################################################################
# Outlier detection resources
######################################################################
# Results of running population-based metrics filtering.
stratified_metrics = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/outlier_detection/gnomad.exomes.v{version}.stratified_metrics.ht"
        )
        for version in VERSIONS
    },
)

# Results of running regressed metrics filtering.
regressed_metrics = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/outlier_detection/gnomad.exomes.v{version}.regressed_metrics.ht"
        )
        for version in VERSIONS
    },
)


######################################################################
# Other resources
######################################################################
# Number of clinvar variants per sample.
sample_clinvar_count = VersionedTableResource(
    CURRENT_VERSION,
    {
        version: TableResource(
            f"{get_sample_qc_root(version)}/gnomad.exomes.v{version}.clinvar.ht"
        )
        for version in VERSIONS
    },
)
