#trust eks' oidc provider

#fetches TLS certificate of cluster url
data "tls_certificate" "eks" {

  url = aws_eks_cluster.this.identity[0].oidc[0].issuer

}

resource "aws_iam_openid_connect_provider" "eks" {

  #trusted issuer url
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
  
  #incoming tokens from provider are meant for sts
  client_id_list = ["sts.amazonaws.com"]

  #used to verify issue
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]


}
