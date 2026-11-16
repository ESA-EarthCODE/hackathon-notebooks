import os
import json
import nbformat
import yaml
import subprocess
from urllib.parse import urlparse
import pathlib
import re
import base64
from PIL import Image
from io import BytesIO

ROOT_DIR = os.path.abspath(".")
OUTPUT_FILE = "notebooks.json"
NOTEBOOK_DIR = "notebooks"
SUBMODULE_ROOT = "external_notebooks"
IGNORE_FOLDERS = ["venv", ".git", ".github", "_build", "_data", "dist"]
DEF_ORG = "eoxhub-workspaces"
DEF_REPO = "eoxhub-notebooks"
FALLBACK_IMAGE = "build/_assets/previews/eoxhub_jupyter.jpg"

def extract_last_image(
    nb,
    notebook_rel_path,
    output_dir="_build/html/build/_assets/previews",
    target_width=300,
):
    os.makedirs(output_dir, exist_ok=True)
    found_images = []
    # Check markdown cells for images
    for cell in nb.cells:
        if cell.cell_type == "markdown":
            lines = cell.source.splitlines()
            for line in lines:
                # Match Markdown image: ![alt](path)
                md_img = re.findall(r"!\[.*?\]\((.*?)\)", line)
                if md_img:
                    found_images.extend(md_img)
                # Match MyST figure directive: :::{figure} ./image.png
                myst_img = re.findall(r":::\{figure\}\s+(.*?)\s*$", line)
                if myst_img:
                    found_images.extend(myst_img)
    
    if found_images:
        last_image_rel = found_images[-1].strip()
        notebook_dir = os.path.dirname(notebook_rel_path)
        image_abs_path = os.path.normpath(os.path.join(notebook_dir, last_image_rel))
        print(f"[info] Found image: {image_abs_path}")

        if os.path.exists(image_abs_path):
            try:
                with Image.open(image_abs_path) as img:
                    # Resize while preserving aspect ratio
                    w_percent = target_width / float(img.size[0])
                    h_size = int(float(img.size[1]) * w_percent)
                    img = img.resize((target_width, h_size), Image.LANCZOS)

                    # Save to unique file
                    image_name = notebook_rel_path.replace("/", "_").replace(
                        ".ipynb", "_preview.png"
                    )
                    output_path = os.path.join(output_dir, image_name)
                    img.save(output_path)
                    relpath = os.path.join("build/_assets/previews", image_name)
                    return os.path.relpath(relpath, start=".").replace("\\", "/")
            except Exception as e:
                print(
                    f"[warn] Couldn't load/resize MyST image for {notebook_rel_path}: {e}"
                )

    # If no markdown images, check code output
    for cell in reversed(nb.cells):
        if cell.cell_type == "code":
            for output in reversed(cell.get("outputs", [])):
                data = output.get("data", {})
                if "image/png" in data:
                    b64 = data["image/png"]
                    image_bytes = base64.b64decode(b64)

                    try:
                        # Load image from bytes
                        image = Image.open(BytesIO(image_bytes))
                        # Resize while maintaining aspect ratio
                        w_percent = target_width / float(image.size[0])
                        h_size = int(float(image.size[1]) * w_percent)
                        image = image.resize((target_width, h_size), Image.LANCZOS)

                        # Create a filename based on notebook path
                        base_name = notebook_rel_path.replace("/", "_").replace(
                            ".ipynb", "_preview.png"
                        )
                        image_path = os.path.join(output_dir, base_name)
                        image.save(image_path)
                        relpath = os.path.join("build/_assets/previews", base_name)

                        return os.path.relpath(relpath, start=".").replace("\\", "/")
                    except Exception as e:
                        print(
                            f"[warn] Failed to process image in {notebook_rel_path}: {e}"
                        )
                        return None
    return None

def parse_gitmodules():
    """Parse .gitmodules to map paths to remote info."""
    gitmodules_path = os.path.join(ROOT_DIR, ".gitmodules")
    if not os.path.exists(gitmodules_path):
        return {}

    submodules = {}
    current = {}

    with open(gitmodules_path, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("[submodule"):
                if current:
                    submodules[current["path"]] = current["url"]
                current = {}
            elif "=" in line:
                key, value = [x.strip() for x in line.split("=", 1)]
                current[key] = value
        if current:
            submodules[current["path"]] = current["url"]

    # Convert to path → { org, repo }
    result = {}
    for path, url in submodules.items():
        if url.endswith(".git"):
            url = url[:-4]
        if url.startswith("git@"):
            url = url.replace(":", "/").replace("git@", "https://")
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2:
            norm_path = os.path.normpath(path)
            result[norm_path] = {
                "org": parts[0],
                "repo": parts[1],
                "url": url
            }

    return result

def get_git_remote_info(repo_path):
    try:
        print(repo_path)
        url = subprocess.check_output(
            ["git", "-C", repo_path, "config", "--get", "remote.origin.url"],
            text=True
        ).strip()
        print(url)
        if url.endswith(".git"):
            url = url[:-4]
        if url.startswith("git@"):
            url = url.replace(":", "/").replace("git@", "https://")
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2:
            return {"org": parts[0], "repo": parts[1], "url": url}
    except Exception as e:
        print(f"[warn] Could not get git remote info from {repo_path}: {e}")
    return {"org": DEF_ORG, "repo": DEF_REPO, "url": url}

def extract_frontmatter(notebook_path):
    try:
        nb = nbformat.read(notebook_path, as_version=4)
        
        # 1. Check for notebook-level metadata (common with Jupytext)
        if 'frontmatter' in nb.metadata:
            return nb.metadata.get('frontmatter', {})
        # MyST/Jupyter Book sometimes stores it here
        if 'jupytext' in nb.metadata and 'jupytext_version' in nb.metadata.jupytext:
             if 'kernelspec' in nb.metadata:
                del nb.metadata["kernelspec"]
             if 'jupytext' in nb.metadata:
                del nb.metadata["jupytext"]
             return dict(nb.metadata)

        # 2. Check metadata of the first cell
        if nb.cells and nb.cells[0].metadata:
            return nb.cells[0].metadata

        # 3. Fallback to YAML frontmatter in the first markdown cell
        if nb.cells and nb.cells[0].cell_type == 'markdown':
            content = nb.cells[0].source
            if content.strip().startswith('---'):
                block = content.split('---')[1]
                return yaml.safe_load(block)
    except Exception as e:
        print(f"[warn] Failed to extract frontmatter from {notebook_path}: {e}")
    return {}

def myst_url_sanitation(url):
    clean_url = url.replace(".ipynb", "").replace(".md", "").replace(".MD", "")
    parts = clean_url.split("/")
    clean_parts = []
    for part in parts:
        cleaned = re.sub(r'^\d+[_\-]', '', part).lower()
        cleaned = re.sub(r'[^a-z0-9]+', '-', cleaned).strip('-')
        if cleaned:
            clean_parts.append(cleaned)
    return "/".join(clean_parts)

def extract_title_from_first_header(nb):
    for cell in nb.cells:
        if cell.cell_type == "markdown":
            lines = cell.source.splitlines()
            for line in lines:
                match = re.match(r'^\s*#\s+(.*)', line)
                if match:
                    return match.group(1).strip()
    return None

def get_submodule_repo_title(sub_path, git_info):
    """Determine a human-readable title for the submodule repository."""
    for readme_name in ["README.md", "README.MD", "readme.md"]:
        readme_path = os.path.join(sub_path, readme_name)
        if os.path.exists(readme_path):
            try:
                with open(readme_path, "r", encoding="utf-8") as f:
                    for line in f:
                        match = re.match(r'^\s*#\s+(.*)', line)
                        if match:
                            title = match.group(1).strip()
                            title = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', title)
                            if title:
                                return title
            except Exception:
                pass
    repo_name = git_info.get("repo") or os.path.basename(sub_path)
    return repo_name.replace("-", " ").replace("_", " ").title()

def extract_tags(rel_path_in_repo, meta):
    """Extract tags from directory hierarchy (stripping number prefixes) and notebook metadata."""
    tags = []
    meta_tags = meta.get("tags") or meta.get("keywords") or []
    if isinstance(meta_tags, list):
        for t in meta_tags:
            if isinstance(t, str) and t.strip():
                tags.append(t.strip())

    dir_name = os.path.dirname(rel_path_in_repo)
    if dir_name:
        parts = pathlib.Path(dir_name).parts
        for part in parts:
            if part in IGNORE_FOLDERS or part in [".", ""]:
                continue
            cleaned_part = re.sub(r'^\d+[_\-\s]*', '', part)
            if cleaned_part:
                tag_label = cleaned_part.replace("_", " ").replace("-", " ").title()
                if tag_label and tag_label not in tags:
                    tags.append(tag_label)

    return tags

def discover_submodules(submodules_root, submodules_map):
    """Find all submodule directories under submodules_root."""
    submodule_paths = []
    if not os.path.exists(submodules_root):
        return submodule_paths

    for root, dirs, _ in os.walk(submodules_root):
        rel_to_project = os.path.normpath(os.path.relpath(root, ROOT_DIR))
        is_sub = rel_to_project in submodules_map or os.path.exists(os.path.join(root, ".git"))
        if is_sub and root != submodules_root:
            submodule_paths.append(root)
            dirs.clear()
            continue

        if root == submodules_root:
            for d in list(dirs):
                child_path = os.path.join(root, d)
                child_rel = os.path.normpath(os.path.relpath(child_path, ROOT_DIR))
                if child_rel in submodules_map or os.path.exists(os.path.join(child_path, ".git")):
                    submodule_paths.append(child_path)
                    dirs.remove(d)

    return submodule_paths

def collect_notebooks():
    catalog = []
    project_git_info = get_git_remote_info(ROOT_DIR)
    git_url = project_git_info["url"]
    project_repo_title = project_git_info.get("repo", "Hackathon Notebooks").replace("-", " ").replace("_", " ").title()
    submodules = parse_gitmodules()

    # --- Local notebooks
    local_path = os.path.join(ROOT_DIR, NOTEBOOK_DIR)
    if os.path.exists(local_path):
        for dirpath, _, filenames in os.walk(local_path):
            if any(ignored in dirpath for ignored in IGNORE_FOLDERS):
                continue
            for file in filenames:
                if file.endswith(".ipynb"):
                    abs_path = os.path.join(dirpath, file)
                    rel_path = os.path.relpath(abs_path, ROOT_DIR).replace("\\", "/")
                    rel_path_in_repo = os.path.relpath(abs_path, local_path).replace("\\", "/")
                    meta = extract_frontmatter(abs_path)
                    nb = nbformat.read(abs_path, as_version=4)
                    image = meta.get("image") or extract_last_image(nb, rel_path) or FALLBACK_IMAGE
                    tags = extract_tags(rel_path_in_repo, meta)
                    catalog.append({
                        "title": meta.get("title", extract_title_from_first_header(nb) or os.path.splitext(file)[0].replace("_", " ")),
                        "description": meta.get("description", ""),
                        "metadata": meta,
                        "image": image,
                        "link": myst_url_sanitation(rel_path.replace(".ipynb", "")),
                        "topic": project_repo_title,
                        "tags": tags,
                        "org": DEF_ORG,
                        "repo": DEF_REPO,
                        "source": "local",
                        "path": rel_path,
                        "gitpuller": f"https://{{JHUB_HOST}}/hub/user-redirect/git-pull?repo={git_url}&urlpath=lab/tree/{DEF_REPO}/{rel_path}&branch=main",
                    })

    # --- Submodule notebooks
    submodules_root = os.path.join(ROOT_DIR, SUBMODULE_ROOT)
    submodule_dirs = discover_submodules(submodules_root, submodules)

    for sub_path in submodule_dirs:
        sub_rel = os.path.relpath(sub_path, ROOT_DIR).replace("\\", "/")
        git_info = submodules.get(os.path.normpath(sub_rel))
        if not git_info:
            git_info = get_git_remote_info(sub_path)
        sub_git_url = git_info.get("url")
        if not sub_git_url:
            print(f"[warn] Could not determine git URL for submodule {sub_rel}, skipping")
            continue

        repo_title = get_submodule_repo_title(sub_path, git_info)
        sub_repo_name = git_info.get("repo") or os.path.basename(sub_path)

        for dirpath, _, filenames in os.walk(sub_path):
            if any(ignored in dirpath for ignored in IGNORE_FOLDERS):
                continue
            for file in filenames:
                if file.endswith(".ipynb"):
                    abs_path = os.path.join(dirpath, file)
                    rel_path = os.path.relpath(abs_path, ROOT_DIR).replace("\\", "/")
                    repo_path = os.path.relpath(abs_path, sub_path).replace("\\", "/")
                    meta = extract_frontmatter(abs_path)
                    nb = nbformat.read(abs_path, as_version=4)
                    image = meta.get("image") or extract_last_image(nb, rel_path) or FALLBACK_IMAGE
                    tags = extract_tags(repo_path, meta)
                    catalog.append({
                        "title": meta.get("title", extract_title_from_first_header(nb) or os.path.splitext(file)[0].replace("_", " ")),
                        "description": meta.get("description", ""),
                        "metadata": meta,
                        "image": image,
                        "link": myst_url_sanitation(rel_path.replace(".ipynb", "")),
                        "topic": repo_title,
                        "tags": tags,
                        "org": git_info.get("org"),
                        "repo": sub_repo_name,
                        "source": "submodule",
                        "path": rel_path,
                        "gitpuller": f"https://{{JHUB_HOST}}/hub/user-redirect/git-pull?repo={sub_git_url}&urlpath=lab/tree/{sub_repo_name}/{repo_path}&branch=main",
                    })

    return catalog

if __name__ == "__main__":
    notebooks = collect_notebooks()
    with open(OUTPUT_FILE, "w") as f:
        json.dump(notebooks, f, indent=2)
    print(f"✅ Catalog saved to {OUTPUT_FILE}")
