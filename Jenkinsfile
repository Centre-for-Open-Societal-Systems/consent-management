pipeline {
    // The agent that reaches the dev cluster (same one the other registries use).
    agent { label 'vpn-agent2' }

    options {
        // Overlapping runs collide on the helm upgrade ("another operation in progress").
        disableConcurrentBuilds()
    }

    environment {
        AWS_ACCOUNT_ID = "379220350808"
        AWS_REGION     = "ap-south-1"
        ECR_REGISTRY   = "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

        // Dev deployment: the OAN commons-services chart from oan-helm, pinned and
        // checksummed. Do not move it to a newer chart, and never pull a standalone
        // consent-manager chart: the Consent Manager chart that commons-services
        // pins (CONSENT_CHART_PIN) is the version our changes must stay compatible with.
        OAN_HELM_RAW        = "https://raw.githubusercontent.com/Centre-for-Open-Societal-Systems/oan-helm/main"
        HELM_CHART_NAME     = "openg2p-commons-services"
        HELM_CHART_VERSION  = "1.0.0-dev.7"
        HELM_CHART_SHA256   = "804fdc00e96667f954a75f9b56742b8fac32b0745b3ba293019de7c40f235bae"
        CONSENT_CHART_PIN   = "1.0.2"
        COMMONS_RELEASE     = "commons-services"
        COMMONS_NAMESPACE   = "commons"
        // Kubeconfig (Jenkins "Secret file") allowed to upgrade the release in COMMONS_NAMESPACE.
        DEV_KUBECONFIG_CRED_ID = "consent-newdev-kubeconfig"
    }

    stages {
        stage('Checkout') {
            steps { checkout scm }
        }

        stage('Check scripts') {
            steps {
                sh '''
                    set -eu
                    bash -n ci/build-push.sh
                    bash -n ci/deploy-dev.sh
                    python3 -m py_compile ci/manifest_guard.py
                '''
            }
        }

        stage('ECR Login') {
            steps {
                withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-ecr-creds']]) {
                    sh "aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ECR_REGISTRY}"
                }
            }
        }

        stage('Build & Push') {
            // Every push builds and pushes all images. Each image gets dev-<commit12>
            // (immutable). develop also moves the `develop` tag; a git tag build also
            // pushes the tag itself, the pinned release version staging/UAT use.
            steps {
                script {
                    env.IMAGE_TAG = env.GIT_COMMIT.take(12)
                    def extra = env.TAG_NAME ?: (env.BRANCH_NAME == 'develop' ? 'develop' : '-')
                    def images = [
                        [name: 'consent-manager-api',            repo: 'openg2p/commons/consent-manager-api',     dockerfile: 'docker/consent-manager-api/Dockerfile',    context: '.',              info: 'no'],
                        [name: 'consent-manager-ui',             repo: 'openg2p/commons/consent-manager-ui',      dockerfile: 'ui/Dockerfile',                           context: 'ui',             info: 'yes'],
                        [name: 'consent-manager-sanity',         repo: 'openg2p/commons/consent-manager-sanity',  dockerfile: 'docker/consent-manager-sanity/Dockerfile', context: '.',              info: 'no'],
                        [name: 'consent-manager-partner-portal', repo: 'oan/commons/consent-partner-portal',      dockerfile: 'partner-portal/Dockerfile',               context: 'partner-portal', info: 'yes'],
                    ]
                    sh 'rm -rf .build && mkdir -p .build'
                    images.each { i ->
                        sh "ci/build-push.sh ${i.name} ${i.repo} ${i.dockerfile} ${i.context} ${env.IMAGE_TAG} ${extra} ${i.info}"
                    }
                }
            }
        }

        stage('Deploy to Development') {
            // Upgrades the existing commons-services release only (never installs),
            // changing nothing but the Consent Manager image references. On a failed
            // rollout the release goes back to its last deployed revision and the
            // build fails; pods/events/logs are saved to deploy-failure-<build>.txt.
            when { branch 'develop' }
            steps {
                withCredentials([file(credentialsId: "${DEV_KUBECONFIG_CRED_ID}", variable: 'KUBECONFIG')]) {
                    sh 'ci/deploy-dev.sh'
                }
            }
            post {
                always {
                    archiveArtifacts artifacts: '.build/images.yaml, .build/dryrun.txt, .build/guard-report.txt, deploy-failure-*.txt', allowEmptyArchive: true
                }
            }
        }
    }

    post {
        failure {
            echo "Consent management ${env.BRANCH_NAME} build/deploy failed. For a deploy failure see the archived deploy-failure-${env.BUILD_NUMBER}.txt; 'helm history ${env.COMMONS_RELEASE} -n ${env.COMMONS_NAMESPACE}' shows the release state."
        }
        always {
            sh 'docker image prune -f || true'
            sh "docker logout ${ECR_REGISTRY} || true"
        }
    }
}
