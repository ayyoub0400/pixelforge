module "vpc" {

  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${var.project_name}-${var.environment}"
  cidr = "10.0.0.0/16"

  #two azs required for eks
  azs             = ["eu-west-2a", "eu-west-2b"]
  private_subnets = ["10.0.16.0/20", "10.0.48.0/20"]
  public_subnets  = ["10.0.0.0/20", "10.0.32.0/20"]

  enable_nat_gateway   = true
  single_nat_gateway   = true
  enable_dns_hostnames = true #required

  public_subnet_tags = {
    "kubernetes.io/role/elb"                                       = "1"
    "kubernetes.io/cluster/${var.project_name}-${var.environment}" = "shared"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb"                              = "1"
    "kubernetes.io/cluster/${var.project_name}-${var.environment}" = "shared"
  }

}
