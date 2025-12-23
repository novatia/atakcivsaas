pipeline {
    agent { label 'Docker_VM' }

    environment {
        IMAGE_NAME = "atakcivserver"
        DOCKERHUB_USER = "nov41337"
        FULL_IMAGE = "${DOCKERHUB_USER}/${IMAGE_NAME}"
    }

    stages {

        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Docker Build & Push') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'NOVA_DOCKER_HUB',
                    usernameVariable: 'DOCKER_USER',
                    passwordVariable: 'DOCKER_PASS'
                )]) {

                    sh '''
                      echo "$DOCKER_PASS" | docker login -u "$DOCKER_USER" --password-stdin

                      docker build --no-cache \
                        -t nov41337/atakcivserver:installer \
                        .

                      docker push nov41337/atakcivserver:installer
                    '''
                }
            }
        }
    }

    post {
        always {
            sh 'docker logout || true'
        }
        success {
            echo "✅ Immagine pubblicata su Docker Hub"
        }
        failure {
            echo "❌ Build o push falliti"
        }
    }
}
