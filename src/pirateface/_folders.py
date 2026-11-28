"""The author--model folder convention: both the Hugging Face cache (models--<author>--<model>) and
pf's own seed folders (<author>--<model>) encode a repo as author--model."""


def repo_model(repo):
    """The model part of a repo id (the author/model suffix)."""
    return repo.split("/")[1]


def repo_folder(repo):
    """The author--model folder name for a repo (the HF cache and the seed folder's shared naming)."""
    return repo.replace("/", "--")


def folder_repo(folder):
    """The author/model repo an HF cache folder name (models--<author>--<model>) names. The models-- prefix is
    stripped first, then only the first -- is a separator (so an author named "models" parses correctly)."""
    return folder.removeprefix("models--").replace("--", "/", 1)
