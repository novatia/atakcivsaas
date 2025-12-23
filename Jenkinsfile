stage('Docker Build & Push') {
  steps {
    script {
      withCredentials([usernamePassword(
        credentialsId: 'NOVA_DOCKER_HUB',
        usernameVariable: 'DOCKER_USER',
        passwordVariable: 'DOCKER_PASS'
      )]) {

        sh """
          echo "\$DOCKER_PASS" | docker login -u "\$DOCKER_USER" --password-stdin

          docker build --no-cache \
            -t nov41337/atakcivserver:installer \
            .

          docker push nov41337/atakcivserver:installer
        """
      }
    }
  }
}
