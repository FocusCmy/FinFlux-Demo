ARG BASE_IMAGE=higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-qwenpaw-worker@sha256:5a8c60926009551f7ce555f657d63c8791450196a79ab41ba8bafd2e1bd51834
FROM ${BASE_IMAGE}

USER root
WORKDIR /opt/finflux-gateway
COPY app/model_budget_gateway.py /opt/finflux-gateway/model_budget_gateway.py

EXPOSE 8090
ENTRYPOINT ["python3", "/opt/finflux-gateway/model_budget_gateway.py"]
