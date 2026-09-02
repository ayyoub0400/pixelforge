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



#allow api role assumption from cluster
data "aws_iam_policy_document" "assume_from_cluster_api" {

  statement {

    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {

      type = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]

    }
    
    condition {

      test = "StringEquals"
      #token has to be intended for aws sts
      variable  = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud"
      values = ["sts.amazonaws.com"]
    }
    condition {

      test = "StringEquals"
      #token has to be inteded for api role
      variable = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub"
      values = ["system:serviceaccount:pixelforge:api"]
    }

  }

}

data "aws_iam_policy_document" "assume_from_cluster_worker" {

  statement {

    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {

      type = "Federated"
      identifiers = [aws_iam_openid_connect_provider.eks.arn]

    }
    
    condition {

      test = "StringEquals"
      #token has to be intended for aws sts
      variable  = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:aud"
      values = ["sts.amazonaws.com"]
    }
    condition {

      test = "StringEquals"
      #token has to be inteded for api role
      variable = "${replace(aws_iam_openid_connect_provider.eks.url, "https://", "")}:sub"
      values = ["system:serviceaccount:pixelforge:worker"]
    }

  }

}
