FROM jupyter/scipy-notebook:aarch64-ubuntu-22.04

USER root
WORKDIR /tmp

RUN apt-get update && apt-get install -y libimage-exiftool-perl=12.40+dfsg-1

USER ${NB_USER}
WORKDIR ${HOME}

COPY requirements.txt /tmp
RUN pip install -r /tmp/requirements.txt

COPY jupyter_notebook_config.py /home/jovyan/.jupyter/

RUN jupyter labextension disable "@jupyterlab/apputils-extension:announcements"

ENTRYPOINT ["start-notebook.py"]
CMD ["--LabApp.check_for_updates_class=jupyterlab.NeverCheckForUpdate"]
