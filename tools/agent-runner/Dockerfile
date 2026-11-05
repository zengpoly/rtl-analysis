FROM ubuntu:24.04 AS common

ARG CODEX_VERSION=0.153.4

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git jq nodejs npm python3 python3-venv ripgrep \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

COPY tools/agent-runner/proxy_bridge.py /opt/agent-runner/proxy_bridge.py
COPY tools/agent-runner/runner_entry.py /opt/agent-runner/runner_entry.py
COPY tools/agent-runner/entrypoint.sh /opt/agent-runner/entrypoint.sh

RUN chmod 0755 /opt/agent-runner/entrypoint.sh \
    && mkdir -p /workspace/source /workspace/input /workspace/scratch /workspace/output /codex-home /run/agent-proxy

WORKDIR /workspace/scratch
ENTRYPOINT ["/opt/agent-runner/entrypoint.sh"]

FROM common AS treatment-a
ENV AGENT_RUNNER_IMAGE_TREATMENT=A

FROM common AS tool-runtime

ARG PYSLANG_WHEEL_URL=https://files.pythonhosted.org/packages/77/29/c677c2b3f7b3b8dfe19341849afb8fda288f7a984df1a8bd4ad04d58560f/pyslang-11.0.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl
ARG PYSLANG_WHEEL_SHA256=a964ac225b5f3e92a57459673c17634884184ae3091a76f8cf5e0b9e2aeca8b2

RUN python3 -m venv /opt/agent-runner/venv \
    && curl --fail --location --silent --show-error "$PYSLANG_WHEEL_URL" \
        -o /tmp/pyslang-11.0.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl \
    && echo "$PYSLANG_WHEEL_SHA256  /tmp/pyslang-11.0.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl" \
        | sha256sum --check - \
    && /opt/agent-runner/venv/bin/pip install --disable-pip-version-check --no-deps \
        /tmp/pyslang-11.0.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl \
    && rm -f /tmp/pyslang-11.0.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl

COPY src/rtl-pack/lib/rtl_query_runtime.py /opt/rtl-analysis-agent/lib/rtl_query_runtime.py
COPY src/rtl-pack/lib/rtl_knowledge_prototype.py /opt/rtl-analysis-agent/lib/rtl_knowledge_prototype.py
COPY src/rtl-pack/config/knowledge-contract-v01-entities.json /opt/rtl-analysis-agent/config/knowledge-contract-v01-entities.json
COPY src/rtl-pack/config/knowledge-contract-v01-facts.json /opt/rtl-analysis-agent/config/knowledge-contract-v01-facts.json
COPY src/rtl-pack/config/knowledge-contract-v01-ai.json /opt/rtl-analysis-agent/config/knowledge-contract-v01-ai.json
COPY tools/agent-runner/bin/rtlq /opt/agent-runner/bin/rtlq
COPY tools/agent-runner/runtime_server.py /opt/agent-runner/runtime_server.py
RUN chmod 0755 /opt/agent-runner/bin/rtlq

ENV PATH=/opt/agent-runner/venv/bin:/opt/agent-runner/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

FROM tool-runtime AS treatment-b
ENV AGENT_RUNNER_IMAGE_TREATMENT=B

FROM tool-runtime AS treatment-c
RUN mkdir -p /workspace/skills
ENV AGENT_RUNNER_IMAGE_TREATMENT=C
