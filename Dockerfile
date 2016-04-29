FROM python:2.7
COPY requirements.txt /opt/mysrc/
COPY fabfile.py /opt/mysrc/
VOLUME /opt/mysrc /opt/mfst
RUN pip install -r /opt/mysrc/requirements.txt
WORKDIR /opt/mysrc
CMD fab list_comps
