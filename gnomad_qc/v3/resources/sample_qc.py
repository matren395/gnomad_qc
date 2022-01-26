import hail as hl
from gnomad.resources.resource_utils import (
    MatrixTableResource,
    TableResource,
    VersionedMatrixTableResource,
    VersionedTableResource,
)
from gnomad.sample_qc.relatedness import get_relationship_expr

from gnomad_qc.v3.resources.constants import (
    CURRENT_VERSION,
    VERSIONS,
)


def get_sample_qc_root(version: str = CURRENT_VERSION, mt: bool = False) -> str:
    """
    Return path to sample QC root folder

    :param version: Version of sample QC path to return
    :param mt: Whether path is for a MatrixTable, default is False
    :return: Root to sample QC path
    """
    return f"gs://gnomad/sample_qc/{'mt' if mt else 'ht'}/genomes_v{version}"


def get_sample_qc(strat: str = "all") -> VersionedTableResource:
    """
    Gets sample QC annotations generated by Hail for the specified stratification:
        - bi_allelic
        - multi_allelic
        - all

    :param strat: Which stratification to return
    :return: Sample QC table
    """
    return VersionedTableResource(
        CURRENT_VERSION,
        {
            release: TableResource(
                f"{get_sample_qc_root(release)}/sample_qc_{strat}.ht"
            )
            for release in VERSIONS
        },
    )


def _get_ancestry_pca_ht_path(
    part: str,
    version: str = CURRENT_VERSION,
    include_unreleasable_samples: bool = False,
) -> str:
    """
    Helper function to get path to files related to ancestry PCA

    :param part: String indicating the type of PCA file to return (loadings, eigenvalues, or scores)
    :param version: Version of sample QC path to return
    :param include_unreleasable_samples: Whether the file includes PCA info for unreleasable samples
    :return: Path to requested ancestry PCA file
    """
    return "{}/gnomad_v{}_pca_{}{}.ht".format(
        get_sample_qc_root(version),
        version,
        part,
        "_with_unreleasable_samples" if include_unreleasable_samples else "",
    )


def ancestry_pca_loadings(
    include_unreleasable_samples: bool = False,
) -> VersionedTableResource:
    """
    Gets the ancestry PCA loadings VersionedTableResource

    :param include_unreleasable_samples: Whether to get the PCA that included unreleasable in training
    :return: Ancestry PCA loadings
    """
    return VersionedTableResource(
        CURRENT_VERSION,
        {
            release: TableResource(
                _get_ancestry_pca_ht_path(
                    "loadings", release, include_unreleasable_samples
                )
            )
            for release in VERSIONS
        },
    )


def ancestry_pca_scores(
    include_unreleasable_samples: bool = False,
) -> VersionedTableResource:
    """
    Gets the ancestry PCA scores VersionedTableResource

    :param include_unreleasable_samples: Whether to get the PCA that included unreleasable in training
    :return: Ancestry PCA scores
    """
    return VersionedTableResource(
        CURRENT_VERSION,
        {
            release: TableResource(
                _get_ancestry_pca_ht_path(
                    "scores", release, include_unreleasable_samples
                )
            )
            for release in VERSIONS
        },
    )


def ancestry_pca_eigenvalues(
    include_unreleasable_samples: bool = False,
) -> VersionedTableResource:
    """
    Gets the ancestry PCA eigenvalues VersionedTableResource

    :param include_unreleasable_samples: Whether to get the PCA that included unreleasable in training
    :return: Ancestry PCA eigenvalues
    """
    return VersionedTableResource(
        CURRENT_VERSION,
        {
            release: TableResource(
                _get_ancestry_pca_ht_path(
                    "eigenvalues", release, include_unreleasable_samples
                )
            )
            for release in VERSIONS
        },
    )


def get_relatedness_annotated_ht() -> hl.Table:
    """
    relatedness table annotated with get_relationship_expr.

    :return: Annotated relatedness table
    """
    relatedness_ht = relatedness.ht()
    return relatedness_ht.annotate(
        relationship=get_relationship_expr(
            kin_expr=relatedness_ht.kin,
            ibd0_expr=relatedness_ht.ibd0,
            ibd1_expr=relatedness_ht.ibd1,
            ibd2_expr=relatedness_ht.ibd2,
        )
    )


# QC Sites (gnomAD v2 QC sites, lifted over)
gnomad_v2_qc_sites = TableResource(
    "gs://gcp-public-data--gnomad/resources/grch38/gnomad_v2_qc_sites_b38.ht"
)

# Dense MT of samples at QC sites
qc = VersionedMatrixTableResource(
    CURRENT_VERSION,
    {
        release: MatrixTableResource(
            f"gs://gnomad/sample_qc/mt/genomes_v{release}/gnomad_v{release}_qc_mt_v2_sites_dense.mt"
        )
        for release in VERSIONS
    },
)

# PC relate PCA scores
pc_relate_pca_scores = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_qc_mt_v2_sites_pc_scores.ht"
        )
        for release in VERSIONS
    },
)

# PC relate results
relatedness = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_qc_mt_v2_sites_relatedness.ht"
        )
        for release in VERSIONS
    },
)

# Sex imputation results
sex = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_sex.ht"
        )
        for release in VERSIONS
    },
)

# Samples to drop for PCA due to them being related
pca_related_samples_to_drop = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_related_samples_to_drop_for_pca.ht"
        )
        for release in VERSIONS
    },
)

# Related samples to drop for release
release_related_samples_to_drop = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_related_release_samples_to_drop.ht"
        )
        for release in VERSIONS
    },
)

# Sample inbreeding
sample_inbreeding = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_inbreeding.ht"
        )
        for release in VERSIONS
    },
)

# Number of clinvar variants per sample
sample_clinvar_count = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_clinvar.ht"
        )
        for release in VERSIONS
        if release != "3"
    },
)

# Inferred sample populations
pop = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_pop.ht"
        )
        for release in VERSIONS
    },
)


def pop_tsv_path(version: str = CURRENT_VERSION) -> str:
    """
    Path to tab delimited file indicating inferred sample populations

    :param version: gnomAD Version
    :return: String path to sample populations
    """
    return f"gs://gnomad/sample_qc/temp/genomes_v{version}/gnomad_v{version}_RF_pop_assignments.txt.gz"


def pop_rf_path(version: str = CURRENT_VERSION) -> str:
    """
    Path to RF model used for inferring sample populations

    :param version: gnomAD Version
    :return: String path to sample pop RF model
    """
    return f"gs://gnomad/sample_qc/temp/genomes_v{version}/gnomad_v{version}_pop.RF_fit.pickle"


def _import_all_hgdp_tgp_pc_scores():
    pca_preoutlier_global_ht = hl.import_table(
        "gs://hgdp-1kg/pca_preoutlier/global*_scores.txt.bgz"
    ).key_by("s")
    pca_postoutlier_global_ht = hl.import_table(
        "gs://hgdp-1kg/pca_postoutlier/global*_scores.txt.bgz"
    ).key_by("s")
    n_pcs = max(
        [int(r.strip("PC")) for r in pca_preoutlier_global_ht.row.keys() if "PC" in r]
    )

    pca_preoutlier_subcont_ht = hl.import_table(
        f"gs://hgdp-1kg/pca_preoutlier/subcont_pca/subcont_pca_*_*scores.txt.bgz",
        impute=True,
    ).key_by("s")
    pca_postoutlier_subcont_ht = hl.import_table(
        f"gs://hgdp-1kg/pca_postoutlier/subcont_pca/subcont_pca_*_*scores.txt.bgz",
        impute=True,
    ).key_by("s")
    hgdp_tgp_pca_ht = pca_preoutlier_subcont_ht.select(
        pca_scores=[pca_preoutlier_subcont_ht[f"PC{pc + 1}"] for pc in range(n_pcs)],
        pca_scores_outliers_removed=[
            pca_postoutlier_subcont_ht[pca_preoutlier_subcont_ht.key][f"PC{pc + 1}"]
            for pc in range(n_pcs)
        ],
        pca_preoutlier_global_scores=[
            hl.float(
                pca_preoutlier_global_ht[pca_preoutlier_subcont_ht.key][f"PC{pc + 1}"]
            )
            for pc in range(n_pcs)
        ],
        pca_postoutlier_global_scores=[
            hl.float(
                pca_postoutlier_global_ht[pca_preoutlier_subcont_ht.key][f"PC{pc + 1}"]
            )
            for pc in range(n_pcs)
        ],
    )

    return hgdp_tgp_pca_ht


def _import_related_samples_to_drop(**kwargs):
    ht = hl.import_table(**kwargs)
    ht = ht.key_by(s=ht.f0)

    return ht


# Hard-filtered samples
hard_filtered_samples = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_hard_filtered_samples.ht"
        )
        for release in VERSIONS
    },
)

# Results of running population-based metrics filtering
# Not used for v3 release (regresed metrics used instead)
stratified_metrics = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_stratified_metrics.ht"
        )
        for release in VERSIONS
    },
)

# Results of running regressed metrics filtering
regressed_metrics = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_regressed_metrics.ht"
        )
        for release in VERSIONS
    },
)

# Ranking of all samples based on quality metrics. Used to remove relateds for PCA.
pca_samples_rankings = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_pca_samples_ranking.ht"
        )
        for release in VERSIONS
    },
)

# Ranking of all release samples based on quality metrics. Used to remove relateds for release.
release_samples_rankings = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_release_samples_ranking.ht"
        )
        for release in VERSIONS
    },
)

# Picard metrics
picard_metrics = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_picard_metrics.ht"
        )
        for release in VERSIONS
    },
)

# Duplicated (or twin) samples
duplicates = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad_v{release}_duplicates.ht"
        )
        for release in VERSIONS
    },
)

# PC relate scores for the sample set that overlaps with v2 samples
v2_v3_pc_relate_pca_scores = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad__v2_v{release}_release_pca_scores.ht"
        )
        for release in VERSIONS
    },
)

# Relatedness information for the sample set that overlaps with v2 samples
v2_v3_relatedness = VersionedTableResource(
    CURRENT_VERSION,
    {
        release: TableResource(
            f"{get_sample_qc_root(release)}/gnomad__v2_v{release}_release_relatedness.ht"
        )
        for release in VERSIONS
    },
)

# Table with HGDP + 1KG/TGP metadata from Alicia Martin's group sample QC
hgdp_tgp_meta = TableResource(
    path="gs://gnomad/sample_qc/ht/genomes_v3.1/hgdp_tgp_additional_sample_metadata.ht"
)

# Table with the set of outliers found by Alicia Martin's group during pop specific PCA analyses as well as one duplicate sample
hgdp_tgp_pop_outliers = TableResource(
    path="gs://gnomad/sample_qc/ht/gnomad.genomes.v3.1.hgdp_tgp_pop_outlier.ht",
    import_func=hl.import_table,
    import_args={
        "paths": "gs://hgdp-1kg/pca_outliers.tsv",
        "impute": True,
        "key": "s",
    },
)

# Table with HGDP + 1KG/TGP relatedness information from Alicia Martin's group sample QC
hgdp_tgp_relatedness = TableResource(path="gs://hgdp-1kg/relatedness_all_metrics.ht")

# Table with HGDP + 1KG/TGP related samples to drop from Alicia Martin's group sample QC
hgdp_tgp_related_samples_to_drop = TableResource(
    path="gs://gnomad/sample_qc/ht/hgdp_tgp_related_samples_to_drop.ht",
    import_func=_import_related_samples_to_drop,
    import_args={
        "paths": "gs://hgdp-1kg/related_sample_ids.txt",
        "impute": True,
        "no_header": True,
    },
)

# Table with HGDP + 1KG/TGP global and subcontinental PCA scores before and after removing outliers
hgdp_tgp_pcs = VersionedTableResource(
    default_version="3.1",
    versions={
        "3.1": TableResource(
            path="gs://gnomad/sample_qc/ht/hgdp_tgp_pca_scores.ht",
            import_func=_import_all_hgdp_tgp_pc_scores,
        )
    },
)
