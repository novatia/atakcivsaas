pipeline {
	agent {
		label 'Docker_VM'
	}

    environment {
        IMAGE_NAME = "atakcivserver"
        DOCKERHUB_USER = "nov41337"
        REGISTRY = "docker.io"
        FULL_IMAGE = "${DOCKERHUB_USER}/${IMAGE_NAME}"
    }

    stages {

        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Docker Build') {
            steps {
                script {
                    def commitHash = sh(
                        script: "git rev-parse --short HEAD",
                        returnStdout: true
                    ).trim()

                    env.IMAGE_TAG = commitHash
				
					sh 'docker system prune -af'

                    sh """
                        docker build \
                          -t ${FULL_IMAGE}:${IMAGE_TAG} \
                          -t ${FULL_IMAGE}:latest \
                          .
                    """
                }
            }
        }

        stage('Docker Login') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'NOVA_DOCKER_HUB',
                    usernameVariable: 'NOVA_DOCKER_USER',
                    passwordVariable: 'NOVA_DOCKER_PASS'
                )]) {
                    sh """
                        echo "$NOVA_DOCKER_PASS" | docker login \
                          -u "$NOVA_DOCKER_USER" \
                          --password-stdin
                    """
                }
            }
        }

        stage('Docker Push') {
            steps {
                sh """
                    docker push ${FULL_IMAGE}:${IMAGE_TAG}
                    docker push ${FULL_IMAGE}:latest
                """
            }
        }
    }

    post {
        always {
            sh 'docker logout || true'
        }
        success {
            echo "✅ Immagine pushata: ${FULL_IMAGE}:${IMAGE_TAG}"
        }
        failure {
            echo "❌ Build o push falliti"
        }
    }
}
