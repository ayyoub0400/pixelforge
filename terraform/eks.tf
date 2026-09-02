#IAM Role for EKS

#assume policy - who can assume the role
data "aws_iam_policy_document" "eks_cluster_assume" {
  statement {

    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

#the actual role
resource "aws_iam_role" "eks_cluster" {

  name = "${var.project_name}-${var.environment}-eks-cluster"
  #who can assume this
  assume_role_policy = data.aws_iam_policy_document.eks_cluster_assume.json

}

#what policies we are attaching to this role

resource "aws_iam_role_policy_attachment" "eks_cluster" {

  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"

}


#The actual cluster

resource "aws_eks_cluster" "this" {

  name = "${var.project_name}-${var.environment}"

  #giving it our above created role
  role_arn = aws_iam_role.eks_cluster.arn

  version = "1.35"

  #linking to our created vpc
  vpc_config {

    subnet_ids              = concat(module.vpc.private_subnets, module.vpc.public_subnets)
    endpoint_public_access  = true
    endpoint_private_access = false

  }

  #delegating auth to AWS IAM's API
  access_config {
    authentication_mode = "API"
  }

  #only creating when the last step of our IAM role creation is done
  depends_on = [aws_iam_role_policy_attachment.eks_cluster]
}

#specifying access to cluster
resource "aws_eks_access_entry" "self" {
  cluster_name = aws_eks_cluster.this.name
  #runner
  principal_arn = data.aws_caller_identity.current.arn

}

#granting eksadminrole to principal
resource "aws_eks_access_policy_association" "self_admin" {

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = data.aws_caller_identity.current.arn
  #the policy
  policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

}




### eks node group ###


data "aws_iam_policy_document" "node_assume" {

  statement {

    actions = ["sts:AssumeRole"]
    principals {

      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]

    }

  }

}

resource "aws_iam_role" "node" {

  name               = "${var.project_name}-${var.environment}-node"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json

}

#workernodepolicy - allow to join eks cluster
#cni for networking/Private IPs
#ECR role to grab images

resource "aws_iam_role_policy_attachment" "node_worker" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}
resource "aws_iam_role_policy_attachment" "node_cni" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}
resource "aws_iam_role_policy_attachment" "node_ecr" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}


#the actual node group

resource "aws_eks_node_group" "default" {

  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.project_name}-${var.environment}-default"
  node_role_arn   = aws_iam_role.node.arn

  subnet_ids = module.vpc.private_subnets


  #going to be handled by KEDA later
  scaling_config {

    desired_size = 2
    min_size     = 1
    max_size     = 3

  }

  instance_types = ["t3.medium"]
  capacity_type  = "ON_DEMAND"

  depends_on = [
    aws_iam_role_policy_attachment.node_worker,
    aws_iam_role_policy_attachment.node_cni,
    aws_iam_role_policy_attachment.node_ecr,
  ]

}
