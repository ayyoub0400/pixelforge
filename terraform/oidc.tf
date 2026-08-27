#grab github certificate dynamically

data "tls_certificate" "github" {

  url = "https://token.actions.githubusercontent.com/.well-known/openid-configuration"

  #our audience
  client_id_list = ["sts.amazonaws.com"]

  thumbprint_list = [data.tls_certificate.github.certificates[0].sha1_fingerprint]

}

