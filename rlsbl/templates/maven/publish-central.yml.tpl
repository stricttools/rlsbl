name: Publish to Maven Central

on:
  release:
    types: [published]
  workflow_dispatch:
    inputs:
      tag:
        description: "Release tag to publish (e.g. v1.2.3). Overrides the ref for retry dispatch."
        required: false
        type: string

# One publish run per tag: a workflow_dispatch retry at the same tag
# queues behind the in-flight run instead of racing it. A publish is never
# cancelled mid-flight.
concurrency:
  group: publish-${{ inputs.tag || github.ref_name }}
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
        with:
          ref: ${{ inputs.tag || github.event.release.tag_name }}
      - uses: {{action "actions/setup-java"}}
        with:
          distribution: temurin
          java-version: "25"
      - uses: {{action "gradle/actions/setup-gradle"}}
      - name: Install gitleaks
        run: |
          GITLEAKS_VERSION=8.24.3
          curl -sSfL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" | tar xz -C /usr/local/bin gitleaks
      - name: Scan source for secrets
        run: |
          gitleaks dir .
      - name: Check if already published
        id: check-maven-central
        run: |
          GROUP_ID=$(grep '^group' build.gradle.kts 2>/dev/null | sed "s/.*= *[\"']\(.*\)[\"'].*/\1/" || grep '<groupId>' pom.xml 2>/dev/null | head -1 | sed 's/.*<groupId>\(.*\)<\/groupId>.*/\1/')
          ARTIFACT_ID=$(grep '^.*archivesBaseName\|^.*artifactId' build.gradle.kts 2>/dev/null | head -1 | sed "s/.*= *[\"']\(.*\)[\"'].*/\1/" || grep '<artifactId>' pom.xml 2>/dev/null | head -1 | sed 's/.*<artifactId>\(.*\)<\/artifactId>.*/\1/')
          VERSION="${GITHUB_REF_NAME#v}"
          if [ -n "${GROUP_ID}" ] && [ -n "${ARTIFACT_ID}" ]; then
            GROUP_PATH=$(echo "${GROUP_ID}" | tr '.' '/')
            if curl -sf "https://repo1.maven.org/maven2/${GROUP_PATH}/${ARTIFACT_ID}/${VERSION}/${ARTIFACT_ID}-${VERSION}.pom" > /dev/null 2>&1; then
              echo "skip=true" >> "$GITHUB_OUTPUT"
              echo "Already published: ${GROUP_ID}:${ARTIFACT_ID}:${VERSION}"
            fi
          fi
      - run: ./gradlew publishAndReleaseToMavenCentral
        if: steps.check-maven-central.outputs.skip != 'true'
        env:
          ORG_GRADLE_PROJECT_mavenCentralUsername: ${{ secrets.SONATYPE_USERNAME }}
          ORG_GRADLE_PROJECT_mavenCentralPassword: ${{ secrets.SONATYPE_PASSWORD }}
          ORG_GRADLE_PROJECT_signingInMemoryKey: ${{ secrets.GPG_SIGNING_KEY }}
          ORG_GRADLE_PROJECT_signingInMemoryKeyPassword: ${{ secrets.GPG_SIGNING_KEY_PASSWORD }}
