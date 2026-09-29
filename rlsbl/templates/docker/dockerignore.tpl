# Written once by rlsbl scaffold; the file is yours from then on.
#
# The publish workflow builds the image from a checkout of the release tag, so
# every file below would otherwise be in the build context, one `COPY .` away
# from a published image. The upload-private-paths check refuses a release
# whose context carries any of the private paths.
.git
{{docker.privatePathIgnore}}
