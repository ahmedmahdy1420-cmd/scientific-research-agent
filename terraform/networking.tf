# =============================================================================
# VPC: public subnets for the load balancer, private subnets for everything
# else. The database and cache have no route to the internet at all, and the
# application tasks reach out only through NAT.
# =============================================================================

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.availability_zone_count)
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.name}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-igw" }
}

# --- subnets -----------------------------------------------------------------
resource "aws_subnet" "public" {
  count = length(local.azs)

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false # tasks live in private subnets; nothing here needs one

  tags = { Name = "${local.name}-public-${local.azs[count.index]}", Tier = "public" }
}

resource "aws_subnet" "private" {
  count = length(local.azs)

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = local.azs[count.index]

  tags = { Name = "${local.name}-private-${local.azs[count.index]}", Tier = "private" }
}

resource "aws_subnet" "data" {
  count = length(local.azs)

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 20)
  availability_zone = local.azs[count.index]

  tags = { Name = "${local.name}-data-${local.azs[count.index]}", Tier = "data" }
}

# --- NAT ---------------------------------------------------------------------
resource "aws_eip" "nat" {
  count  = var.enable_nat_gateway ? length(local.azs) : 0
  domain = "vpc"
  tags   = { Name = "${local.name}-nat-eip-${count.index}" }
}

resource "aws_nat_gateway" "main" {
  # One NAT per AZ: a single shared NAT is cheaper but becomes a cross-AZ
  # dependency and a single point of failure for outbound model calls.
  count = var.enable_nat_gateway ? length(local.azs) : 0

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  depends_on    = [aws_internet_gateway.main]

  tags = { Name = "${local.name}-nat-${count.index}" }
}

# --- routing -----------------------------------------------------------------
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-rt-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  count  = length(local.azs)
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-rt-private-${count.index}" }
}

resource "aws_route" "private_nat" {
  count = var.enable_nat_gateway ? length(local.azs) : 0

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main[count.index].id
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# The data tier gets a route table with NO default route. RDS and ElastiCache
# have no path off the VPC in either direction.
resource "aws_route_table" "data" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-rt-data" }
}

resource "aws_route_table_association" "data" {
  count          = length(aws_subnet.data)
  subnet_id      = aws_subnet.data[count.index].id
  route_table_id = aws_route_table.data.id
}

# --- VPC endpoints -----------------------------------------------------------
# S3 through a gateway endpoint: document bytes never traverse NAT, which is
# both cheaper and keeps the traffic on the AWS backbone.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = concat(aws_route_table.private[*].id, [aws_route_table.data.id])

  tags = { Name = "${local.name}-vpce-s3" }
}

resource "aws_security_group" "vpc_endpoints" {
  name        = "${local.name}-vpce"
  description = "Interface VPC endpoints"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "HTTPS from application tasks"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  tags = { Name = "${local.name}-vpce-sg" }
}

# Interface endpoints so pulling images, reading secrets and writing logs do
# not depend on NAT being healthy.
resource "aws_vpc_endpoint" "interface" {
  for_each = toset([
    "ecr.api",
    "ecr.dkr",
    "logs",
    "secretsmanager",
  ])

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${local.name}-vpce-${each.value}" }
}

# =============================================================================
# Security groups: each tier accepts traffic only from the tier above it.
# =============================================================================
resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public load balancer"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.allowed_ingress_cidrs
  }

  ingress {
    description = "HTTP (redirected to HTTPS when a certificate is configured)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = var.allowed_ingress_cidrs
  }

  egress {
    description = "To application tasks"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = { Name = "${local.name}-alb-sg" }
}

resource "aws_security_group" "app" {
  name        = "${local.name}-app"
  description = "ECS tasks (API, workers, MCP)"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "API port from the load balancer only"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description = "MCP port from inside the VPC only (never public)"
    from_port   = 8020
    to_port     = 8020
    protocol    = "tcp"
    self        = true
  }

  egress {
    description = "Outbound HTTPS: model provider, S3, ECR, Secrets Manager"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "PostgreSQL"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "Redis"
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = { Name = "${local.name}-app-sg" }
}

resource "aws_security_group" "database" {
  name        = "${local.name}-db"
  description = "RDS PostgreSQL"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "PostgreSQL from application tasks only"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  # No egress rule at all: the database initiates nothing.

  tags = { Name = "${local.name}-db-sg" }
}

resource "aws_security_group" "redis" {
  name        = "${local.name}-redis"
  description = "ElastiCache Redis"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Redis from application tasks only"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  tags = { Name = "${local.name}-redis-sg" }
}

# --- flow logs ---------------------------------------------------------------
resource "aws_flow_log" "vpc" {
  vpc_id                   = aws_vpc.main.id
  traffic_type             = "REJECT" # rejects only: the signal, without the volume
  iam_role_arn             = aws_iam_role.flow_logs.arn
  log_destination          = aws_cloudwatch_log_group.flow_logs.arn
  max_aggregation_interval = 600
}
