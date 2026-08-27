#Defining who can assume our CI role
data "aws_iam_policy_document" "ci_assume" {

  statement {
    #assume a role using OIDC 'WithWebIdentity'
    actions = ["sts:AssumeRoleWithWebIdentity"]


    # who can assume the role
    principals {

      #external identity provider
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]

    }

    condition {

      #what we are testing for
      test = "StringEquals"

      #the value provided - audience
      variable = "token.actions.githubusercontent.com:aud"

      #what we need it to match
      values = ["sts.amazonaws.com"]

    }


    condition {

      #what we are testing for
      test = "StringEquals"

      #the value provided - subject
      variable = "token.actions.githubusercontent.com:sub"

      #subjects are pushes to main and PR request runs
      values = [
        "repo:ayyoub0400/pixelforge:ref:refs/heads/main",
        "repo:ayyoub0400/pixelforge:pull_request",
      ]

    }
  }
}

#What the CI role can do

data "aws_iam_policy_document" "ci" {

  #permission block - docker registry login  
  statement {

    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]

  }

  #permission block - push to repo
  statement {

    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability", # checks if layer has been uploaded 
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:BatchGetImage",
    ]
    resources = [
      aws_ecr_repository.api.arn,
      aws_ecr_repository.worker.arn,
    ]
  }
}

#the actual role to be assumed 
resource "aws_iam_role" "ci" {

  name = "${var.project_name}-${var.environment}-ci"

  #who can assume it - defined in our previous policy
  assume_role_policy = data.aws_iam_policy_document.ci_assume.json

}


#attaching the permission policy attached to the role
resource "aws_iam_role_policy" "ci" {

  name   = "${var.project_name}-${var.environment}-ci"
  role   = aws_iam_role.ci.id
  policy = data.aws_iam_policy_document.ci.json

}

output "ci_role_arn" {

  value = aws_iam_role.ci.arn

}
