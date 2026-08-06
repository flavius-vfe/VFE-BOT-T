# Third-party notices

VFE-BOT-T itself is distributed under the MIT License in `LICENSE`.

The application directly uses these runtime packages:

| Package | Version | License | Included notice |
|---|---:|---|---|
| Docker SDK for Python | 7.2.0 | Apache-2.0 | `licenses/third-party/docker-sdk-python-Apache-2.0.txt` |
| HTTPX | 0.28.1 | BSD-3-Clause | `licenses/third-party/httpx-BSD-3-Clause.txt` |
| PyYAML | 6.0.2 | MIT | `licenses/third-party/pyyaml-MIT.txt` |

These packages install additional transitive dependencies. During every Docker image build, pip creates an installation report and `tools/collect_licenses.py` copies the license and notice files from every package installed by that report into:

```text
/usr/share/licenses/vfe-bot-t/python-packages/
```

The exact installed package list is written to:

```text
/usr/share/licenses/vfe-bot-t/INSTALLED_PACKAGES.txt
```

The build fails if an installed Python dependency does not contain a recognizable license or notice file. This prevents publishing an image with an incomplete generated license bundle.

The `python:3.12-slim` base image also contains Python and Debian components. Their upstream license and copyright files remain in their standard image locations, including `/usr/local/lib/python3.12/LICENSE.txt` and `/usr/share/doc/*/copyright`; this project does not delete them.

No dependency license changes the MIT license of VFE-BOT-T itself. Each dependency remains licensed by its respective copyright holder under the terms included with that dependency.
