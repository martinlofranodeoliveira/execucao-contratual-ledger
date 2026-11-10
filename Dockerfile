FROM python@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f
COPY wheels/ /opt/wheelhouse/
COPY --chown=1000:1000 source/ /opt/admitted-source/
RUN python -m venv /opt/sdk && /opt/sdk/bin/python -I -m pip --isolated --disable-pip-version-check install --no-index --no-cache-dir --no-compile --require-hashes --find-links=/opt/wheelhouse -r /opt/admitted-source/requirements-build.lock
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /tmp
USER 1000:1000
ENTRYPOINT ["/opt/sdk/bin/python","-I","-B","/opt/admitted-source/scripts/native_check.py"]
