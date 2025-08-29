import argparse
import os
import re

import tempfile
import shutil
import subprocess

from langchain.prompts import PromptTemplate
from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_core.output_parsers import StrOutputParser
from langchain_openai import ChatOpenAI


def get_instrumentation_code(path):
    """
    Loads relevant Python files from the specified directory, prioritizing key files
    and limiting content to stay under token limits.
    """
    loader = DirectoryLoader(
        path, 
        glob="**/*.py",
        loader_cls=TextLoader,
        loader_kwargs={'encoding': 'utf-8'},
        show_progress=True,
        use_multithreading=True,
        exclude=["**/tests/**", "**/test_*.py", "**/__pycache__/**"]
    )
    docs = loader.load()
    if not docs:
        return f"Could not find any .py files in '{path}'"

    # Priority order for files - look for these patterns first
    priority_patterns = [
        "__init__.py",
        "instrumentation.py", 
        "patch.py",
        "middleware.py",
        "version.py"
    ]
    
    # Sort docs by priority (high priority files first)
    def get_priority(doc):
        filename = os.path.basename(doc.metadata['source'])
        for i, pattern in enumerate(priority_patterns):
            if pattern in filename:
                return i
        return len(priority_patterns)
    
    docs.sort(key=get_priority)
    
    # Limit content to avoid token limits (roughly 25k tokens = ~100k chars)
    max_chars = 80000  # Conservative limit
    total_chars = 0
    selected_docs = []
    
    for doc in docs:
        doc_chars = len(doc.page_content)
        if total_chars + doc_chars < max_chars:
            selected_docs.append(doc)
            total_chars += doc_chars
        else:
            # If this is a high-priority file, include a truncated version
            if get_priority(doc) < 3:  # First 3 priority patterns
                remaining_chars = max_chars - total_chars - 200  # Leave some buffer
                if remaining_chars > 1000:  # Only if we have meaningful space left
                    truncated_content = doc.page_content[:remaining_chars] + "\n\n... [TRUNCATED]"
                    doc.page_content = truncated_content
                    selected_docs.append(doc)
            break
    
    # Concatenate the content of selected documents
    return "\n\n---\n\n".join(
        [f"# Source: {os.path.basename(doc.metadata['source'])}\n\n{doc.page_content}" for doc in selected_docs]
    )


def extract_env_vars_from_code(source_code: str):
    """
    Extracts environment variable names from the source code.
    """
    return sorted(set(re.findall(r"OTEL_[A-Z0-9_]+", source_code)))


def estimate_tokens(text: str) -> int:
    """Rough estimation of tokens (1 token ≈ 4 characters)"""
    return len(text) // 4


def generate_instrumentation_metadata(instrumentation_dir):
    """Generates a metadata.yaml file for a single instrumentation."""
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)  

    # The example you are happy with, to be used in the prompt
    example_yaml = """
name: FastAPI
instrumentation_name: fastapi
source_href: https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/instrumentation/opentelemetry-instrumentation-fastapi
package_name: opentelemetry-instrumentation-fastapi
stability: stable
description: This instrumentation provides automatic tracing for web requests in FastAPI applications.
settings:
  - property: otel.python.fastapi.enabled
    env: OTEL_PYTHON_FASTAPI_ENABLED
    description: If true, enables the FastAPI instrumentation.
    default: 'true'
    type: boolean
    category: instrumentation
  - property: otel.instrumentation.http.capture_headers.server.request
    env: OTEL_PYTHON_SERVER_REQUEST_HEADERS
    description: A comma-separated list of HTTP request header names to capture.
    default: ''
    type: string
    category: instrumentation
  - property: otel.instrumentation.http.capture_headers.server.response
    env: OTEL_PYTHON_SERVER_RESPONSE_HEADERS
    description: A comma-separated list of HTTP response header names to capture.
    default: ''
    type: string
    category: instrumentation
spans:
  - name: 'HTTP {request.method}'
    kind: SERVER
    attributes:
      - name: http.method
      - name: http.scheme
      - name: http.host
      - name: http.target
      - name: http.server_name
      - name: http.status_code
      - name: http.flavor
      - name: net.host.port
      - name: net.host.name
      - name: http.route
"""

    prompt_template = PromptTemplate(
        input_variables=["instrumentation_name", "source_code", "example_yaml", "env_vars"],
        template="""
        Based on the following source code for the '{instrumentation_name}' instrumentation,
        and using the provided YAML file as an example, please generate a metadata.yaml file.

        These environment variables were detected in the source code and MUST be included
        in the `settings` section with correct descriptions, types, and defaults:
        {env_vars}

        Example YAML:
        
        {example_yaml}
        

        Source Code for '{instrumentation_name}':
        
        {source_code}
        

        Rules:
        - Always include all env vars listed above in `settings`.
        - Use clear and concise descriptions for each property.
        - Infer defaults and types if possible (boolean, string, integer).
        - Follow the structure in the example exactly.
        - Include a `spans` section if relevant, based on HTTP server patterns.
        - Output only valid YAML. Do not add any explanations, comments, or extra text before or after the YAML.

        Generated metadata.yaml:
        """
    )

    chain = prompt_template | llm | StrOutputParser()

    instrumentation_name = os.path.basename(instrumentation_dir)
    source_code = get_instrumentation_code(instrumentation_dir)
    env_vars = extract_env_vars_from_code(source_code)

    # Check token estimate before sending
    prompt_text = prompt_template.format(
        instrumentation_name=instrumentation_name,
        source_code=source_code,
        example_yaml=example_yaml,
        env_vars=env_vars
    )
    
    estimated_tokens = estimate_tokens(prompt_text)
    
    
    if estimated_tokens > 25000:  # Conservative limit for gpt-4o-mini
        max_source_chars = 40000  # Even more conservative
        if len(source_code) > max_source_chars:
            source_code = source_code[:max_source_chars] + "\n\n... [TRUNCATED FOR TOKEN LIMIT]"

    generated_yaml = chain.invoke({
        "instrumentation_name": instrumentation_name,
        "source_code": source_code,
        "example_yaml": example_yaml,
        "env_vars": env_vars
    })



    return generated_yaml



def save_yaml(generated_yaml, instrumentation_dir, yamls_dir):
    """
    Saves YAML to the 'yamls' folder in the current directory, with a filename matching the instrumentation name.
    """
    clean_yaml = generated_yaml.replace("```yaml", "").replace("```", "").strip()
    if "---" in clean_yaml:
        yaml_parts = clean_yaml.split("---")
        for part in yaml_parts:
            part = part.strip()
            if part and ("name:" in part or "instrumentation_name:" in part):
                clean_yaml = part
                break

    instr_name = os.path.basename(instrumentation_dir)
    yamls_dir = os.path.abspath(yamls_dir)
    os.makedirs(yamls_dir, exist_ok=True)
    output_path = os.path.join(yamls_dir, f"{instr_name}.yaml")
    with open(output_path, 'w') as f:
        f.write(clean_yaml)

def clone_repo(repo_url, branch=None):
    temp_dir = tempfile.mkdtemp(prefix="otel-python-contrib-")
    clone_cmd = ["git", "clone"]
    if branch:
        clone_cmd += ["-b", branch]
    clone_cmd += [repo_url, temp_dir]
    subprocess.run(clone_cmd, check=True)
    return temp_dir

def find_instrumentation_dirs(base_dir):
    instr_dirs = []
    instr_root = os.path.join(base_dir, "instrumentation")
    if not os.path.isdir(instr_root):
        return instr_dirs
    for name in os.listdir(instr_root):
        path = os.path.join(instr_root, name)
        if os.path.isdir(path) and name.startswith("opentelemetry-instrumentation-"):
            instr_dirs.append(path)
    return instr_dirs

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clone opentelemetry-python-contrib and generate metadata.yaml for all instrumentations.")
    parser.add_argument("--repo", default="https://github.com/open-telemetry/opentelemetry-python-contrib", help="GitHub repo URL")
    parser.add_argument("--branch", default=None, help="Branch to clone (optional)")
    args = parser.parse_args()

    yamls_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yamls")

    temp_repo_dir = clone_repo(args.repo, args.branch)
    try:
        instr_dirs = find_instrumentation_dirs(temp_repo_dir)
        print(f"Found {len(instr_dirs)} instrumentations.")
        for instr_dir in instr_dirs:
            print(f"Generating metadata for {instr_dir} ...")
            try:
                yaml = generate_instrumentation_metadata(instr_dir)
                save_yaml(yaml, instr_dir, yamls_dir)
            except Exception as e:
                print(f"Failed for {instr_dir}: {e}")
    finally:
        shutil.rmtree(temp_repo_dir)

