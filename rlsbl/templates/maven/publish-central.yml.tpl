name: Publish to Maven Central

on:
  release:
    types: [published]
  workflow_dispatch:

# One publish run per tag ref: a workflow_dispatch retry at the same tag
# queues behind the in-flight run instead of racing it. A publish is never
# cancelled mid-flight.
concurrency:
  group: ${{ github.workflow_ref }}-${{ github.ref }}
  cancel-in-progress: false

jobs:
{{publishGate}}
  publish:
    needs: gate
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: {{action "actions/checkout"}}
      - uses: {{action "actions/setup-java"}}
        with:
          distribution: temurin
          java-version: "25"
      - uses: {{action "gradle/actions/setup-gradle"}}
      - run: ./gradlew publishAndReleaseToMavenCentral
        env:
          ORG_GRADLE_PROJECT_mavenCentralUsername: ${{ secrets.SONATYPE_USERNAME }}
          ORG_GRADLE_PROJECT_mavenCentralPassword: ${{ secrets.SONATYPE_PASSWORD }}
          ORG_GRADLE_PROJECT_signingInMemoryKey: ${{ secrets.GPG_SIGNING_KEY }}
          ORG_GRADLE_PROJECT_signingInMemoryKeyPassword: ${{ secrets.GPG_SIGNING_KEY_PASSWORD }}
