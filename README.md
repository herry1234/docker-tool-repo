# docker-tool-repo
docker image for python tooling for bop
command: 
docker run -it -v /home/herry/workspace/mfst:/opt/mfst tool-repo

inside container
fab prep_with_latest_comps:boproxy,suffix=boptest

or docker run -it -v /home/herry/workspace/mfst:/opt/mfst tool-repo fab prep_with_latest_comps:boproxy,suffix=boptest

