#!/usr/bin/env python3
"""
Simple Metadata Aggregator

Aggregates all metadata.yaml files from instrumentation directories 
and creates a splunk-otel-python metadata.
"""

import os
import yaml
from pathlib import Path
from typing import Dict, List, Any
from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_openai import ChatOpenAI  


def find_all_metadata_files(base_path) -> List[Path]:
    base_path = Path(base_path)
    metadata_files = []
    for item in base_path.iterdir():
        if item.is_dir() and item.name.startswith('opentelemetry-instrumentation-'):
            metadata_file = item / "metadata.yaml"
            if metadata_file.exists():
                metadata_files.append(metadata_file)
    # Sort by directory name
    metadata_files.sort(key=lambda p: p.parent.name)
    return metadata_files


def load_metadata_yaml_text(metadata_path: Path) -> str:
    try:
        return metadata_path.read_text()
    except Exception as e:
        print(f"[ERROR] Could not read {metadata_path}: {e}")
        return ""


def langchain_metadata_yaml(metadata_yaml: str, package_name: str, llm=None) -> Dict[str, Any]:
        """
        Use LLM to convert OpenTelemetry Python instrumentation metadata to classic YAML block style (not Java style).
        """
        if not llm:
                llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
        manual_example = '''
component: Splunk Distribution of OpenTelemetry Python
version: 1.0.0
dependencies:
        - name: OpenTelemetry Api
            version: 1.35.0
            source_href: https://github.com/open-telemetry/opentelemetry-python
            stability: stable
        - name: OpenTelemetry Sdk
            version: 1.35.0
            source_href: https://github.com/open-telemetry/opentelemetry-python
            stability: stable
        - name: OpenTelemetry Propagator B3
            version: 1.35.0
            source_href: https://github.com/open-telemetry/opentelemetry-python
            stability: stable
        - name: OpenTelemetry Exporter Otlp Proto Grpc
            version: 1.35.0
            source_href: https://github.com/open-telemetry/opentelemetry-python
            stability: stable
        - name: OpenTelemetry Exporter Otlp Proto Http
            version: 1.35.0
            source_href: https://github.com/open-telemetry/opentelemetry-python
            stability: stable
        - name: OpenTelemetry Instrumentation
            version: 0.56b0
            source_href: https://github.com/open-telemetry/opentelemetry-python
            stability: experimental
        - name: OpenTelemetry Instrumentation System Metrics
            version: 0.56b0
            source_href: https://github.com/open-telemetry/opentelemetry-python
            stability: experimental
        - name: OpenTelemetry Semantic Conventions
            version: 0.56b0
            source_href: https://github.com/open-telemetry/opentelemetry-python
            stability: experimental
instrumentations:
    - keys:
            - pymemcache
        instrumented_components:
            - name: Pymemcache
                supported_versions: varies
        stability: stable
        support: official
        signals:
            - spans:
                    - span_name: Pymemcache {command}
                        kind: client
                        attributes:
                            - db.system
                            - db.statement
                            - net.peer.name
                            - net.peer.port
                            - net.transport
'''
        prompt = PromptTemplate(
                input_variables=["metadata_yaml", "package_name", "manual_example"],
                template="""
                Convert the following OpenTelemetry Python instrumentation to metadata.yaml. Return only a valid YAML object, using classic YAML block style (no JSON, no markdown, no flow style, no comments).
                The output must start with keys: keys, instrumented_components, stability, support, signals (if present).
                Do not add any explanations or formatting, only pure YAML.

                Here is an example of the desired YAML style:
                ---
                {manual_example}
                ---
                Now convert this instrumentation:
                Package: {package_name}
                metadata.yaml:
                ---
                {metadata_yaml}
                ---
                """
        )
        chain = prompt | llm | StrOutputParser()
        result = chain.invoke({"metadata_yaml": metadata_yaml, "package_name": package_name, "manual_example": manual_example})
        # Remove markdown and JSON wrappers, keep only YAML
        result = result.replace("```yaml", "").replace("```", "").replace("```json", "").strip()
        # Remove any leading or trailing non-YAML lines
        yaml_start = result.find('keys:')
        if yaml_start > 0:
                result = result[yaml_start:]
        try:
                return yaml.safe_load(result)
        except Exception as e:
                print(f"[WARN] LLM output parse error for {package_name}: {e}\nOutput was:\n{result[:200]}...")
                return {}


def load_dependencies_from_pyproject(pyproject_path) -> List[Dict[str, Any]]:
    pyproject_path = Path(pyproject_path)
    if not pyproject_path.exists():
        print(f"[WARN] pyproject.toml not found at {pyproject_path}")
        return []
    try:
        content = pyproject_path.read_text()
        dependencies = []
        in_deps = False
        for line in content.split('\n'):
            if line.strip() == 'dependencies = [':
                in_deps = True
                continue
            elif in_deps and line.strip() == ']':
                break
            elif in_deps and line.strip().startswith('"'):
                dep_line = line.strip().strip(',').strip('"')
                if '==' in dep_line:
                    name, version = dep_line.split('==', 1)
                    component_name = name.replace('-', ' ').title()
                    if 'opentelemetry' in name.lower():
                        component_name = component_name.replace('Opentelemetry', 'OpenTelemetry')
                    dependencies.append({
                        "name": component_name,
                        "version": version,
                        "source_href": f"https://github.com/open-telemetry/opentelemetry-python" if name.startswith('opentelemetry') else "",
                        "stability": "stable" if not any(x in version for x in ['alpha', 'beta', 'b']) else "experimental"
                    })
        return dependencies
    except Exception as e:
        print(f"[WARN] Could not parse pyproject.toml: {e}")
        return []


def main():
    # !!! Change these paths to match your local setup !!!
    instrumentation_dir = "PATH_TO_YOUR_INSTRUMENTATION_DIR"
    pyproject_path = "PATH_TO_YOUR_PYPROJECT_TOML"
    
    dependencies_raw = load_dependencies_from_pyproject(pyproject_path)
    dependencies = []
    for i, dep in enumerate(dependencies_raw, 1):
        dependencies.append(dep)

    metadata_files = find_all_metadata_files(instrumentation_dir)
    print(f"[INFO] Found {len(metadata_files)} instrumentations")
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    instrumentations = []
    total_instr = len(metadata_files)

    for i, metadata_path in enumerate(metadata_files, 1):
        package_name = metadata_path.parent.name
        metadata_yaml = load_metadata_yaml_text(metadata_path)
        if metadata_yaml:
            instr = langchain_metadata_yaml(metadata_yaml, package_name, llm=llm)
            if instr:
                instrumentations.append(instr)
            else:
                print(f"[WARN] No instrumentation generated for {package_name}")
        print(f"Processed instrumentations: {i}/{total_instr}", end='\r')
    if total_instr > 0:
        print()  # Newline after last count
    final_metadata = {
        "component": "Splunk Distribution of OpenTelemetry Python",
        "version": "1.0.0",
        "dependencies": dependencies,
        "instrumentations": instrumentations
    }
    output_path = Path("splunk-otel-python-metadata.yaml")

    # Custom Dumper to force 4-space indent for lists
    class IndentDumper(yaml.SafeDumper):
        def increase_indent(self, flow=False, indentless=False):
            return super(IndentDumper, self).increase_indent(flow, False)

    with open(output_path, 'w') as f:
        yaml.dump(final_metadata, f, default_flow_style=False, sort_keys=False, Dumper=IndentDumper, indent=2)

    print(f"[SUCCESS] Generated metadata: {output_path}")
    print(f"[SUCCESS] {len(dependencies)} dependencies, {len(instrumentations)} instrumentations")


if __name__ == "__main__":
    main()
