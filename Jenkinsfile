pipeline {
    // Validation and image builds run on any build agent; only the deploy stage
    // runs on oan-ci-deploy, the node that reaches New Dev (as in the registries).
    agent any

    options {
        // Overlapping runs collide on the helm upgrade ("another operation in progress").
        disableConcurrentBuilds()
        // Build agents are on-demand spot machines: if one is reclaimed mid-build,
        // run the build again instead of failing it.
        retry(count: 2, conditions: [agent(), nonresumable()])
    }

    environment {
        AWS_ACCOUNT_ID = "379220350808"
        AWS_REGION     = "ap-south-1"
        ECR_REGISTRY   = "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
        // Images: eoan/consent-management/<image>-<CM version>:<version>-<branch>.<build>, named by
        // ci/image_names.py from deployment/charts/openg2p-consent-manager/Chart.yaml, e.g.
        // eoan/consent-management/consent-manager-api-1.0.2:0.0.0-develop.12 (same standard as the registries).

        // Dev deployment: the commons-services release in namespace `commons` on New Dev
        // (the registries cluster). The pipeline upgrades it with the chart version it
        // already runs (fetched from oan-helm) and changes only the Consent Manager
        // images. It never installs, and never pulls a standalone consent-manager chart:
        // commons-services must still pin openg2p-consent-manager CONSENT_CHART_PIN.
        OAN_HELM_RAW        = "https://raw.githubusercontent.com/Centre-for-Open-Societal-Systems/oan-helm/main"
        HELM_CHART_NAME     = "openg2p-commons-services"
        CONSENT_CHART_PIN   = "1.0.2"
        COMMONS_RELEASE     = "commons-services"
        COMMONS_NAMESPACE   = "commons"
        // Kubeconfig (Jenkins "Secret file") for ServiceAccount commons/consent-ci on New Dev
        // (k8s/consent-commons-rbac.yaml): may upgrade the release in COMMONS_NAMESPACE only.
        DEV_KUBECONFIG_CRED_ID = "consent-dev-kubeconfig"
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
                    python3 -m py_compile ci/manifest_guard.py ci/image_names.py
                    python3 -m unittest discover -s ci -p 'test_*.py' -v
                '''
            }
        }

        stage('Build & Push') {
            // Every push builds and pushes all four images under one tag,
            // <version>-<branch>.<build>. Missing ECR repositories are created.
            steps {
                script {
                    env.IMAGE_TAG = sh(returnStdout: true,
                        script: 'python3 ci/image_names.py tag "$BRANCH_NAME" "$BUILD_NUMBER"').trim()
                    echo "Image tag: ${env.IMAGE_TAG}"
                    def images = [
                        [name: 'consent-manager-api',            dockerfile: 'docker/consent-manager-api/Dockerfile',    context: '.',              info: 'no'],
                        [name: 'consent-manager-ui',             dockerfile: 'ui/Dockerfile',                           context: 'ui',             info: 'yes'],
                        [name: 'consent-manager-sanity',         dockerfile: 'docker/consent-manager-sanity/Dockerfile', context: '.',              info: 'no'],
                        [name: 'consent-manager-partner-portal', dockerfile: 'partner-portal/Dockerfile',               context: 'partner-portal', info: 'yes'],
                    ]
                    sh 'rm -rf .build && mkdir -p .build'
                    withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-ecr-creds']]) {
                        sh "aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ECR_REGISTRY}"
                        images.each { i ->
                            sh "ci/build-push.sh ${i.name} ${i.dockerfile} ${i.context} ${i.info}"
                        }
                    }
                }
                stash name: 'deploy', includes: 'ci/**,deploy/**,.build/**'
            }
            post {
                always {
                    sh "docker logout ${ECR_REGISTRY} || true"
                }
            }
        }

        stage('Deploy to Development') {
            // Upgrades the existing commons-services release only (never installs),
            // changing nothing but the Consent Manager image references. On a failed
            // rollout the release goes back to its last deployed revision and the
            // build fails; pods/events/logs are saved to deploy-failure-<build>.txt.
            when {
                beforeAgent true
                branch 'develop'
            }
            agent { label 'oan-ci-deploy' }
            steps {
                sh 'rm -rf ci deploy .build'
                unstash 'deploy'
                withCredentials([file(credentialsId: "${DEV_KUBECONFIG_CRED_ID}", variable: 'KUBECONFIG')]) {
                    sh 'bash ci/deploy-dev.sh'
                }
            }
            post {
                always {
                    archiveArtifacts artifacts: '.build/images.yaml, .build/guard-report.txt, deploy-failure-*.txt', allowEmptyArchive: true
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
        }
    }
}
