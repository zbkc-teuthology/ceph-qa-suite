SHELL=/bin/bash
D=/tmp/stampsdir
VPATH=${D}
TIMEOUT_SERVER_CREATE = 30m
TIMEOUT_BUILD = 220m # 20 minutes short of 4 hours
PKG_REPO=packages-repository
PKG_REPO_OS_TYPE=ubuntu
PKG_REPO_OS_VERSION=14.04
PKG_REPO_USER_DATA=${PKG_REPO_OS_TYPE}-${PKG_REPO_OS_VERSION}-user-data.txt

# We want to extract the first listed IPv4 address!
# Openstack will provide the addresses field in this format:
# "net1-name=ip(, ip)+(; net2-name=ip(, ip)+)+"
# Each IP may be v4 or v6 (including shortened forms and IPv4-mapped-IPv6 forms)
# 1.2.3.4
# 2001:db8:6050:ed4d:f816:3eff:fe48:3b36
# 2001:db8::fe48:3b36
# 2001:db8::1.2.3.4
# Example long-form input:
# private-network=10.10.10.69, 2001:db8:6050:ed4d:f816:3eff:fed1:d9f8;net-name2=2001:db8::fe48:3b36, 2001:db8::1.2.3.4, 1.2.3.4;
# TODO: allow selection of the network instead of taking the first network
# TODO: Support IPv6 in future
define get_ip
$$(openstack server show -f value -c addresses $(1) |perl -pe 's/^[^=]+=([^;]+).*/\1/g; s/[ ,]/\n/g; ' |grep -v -e ':' -e '^$$' |head -n1)
endef

MY_IP=$(shell hostname -I | cut -f1 -d' ')

${HOME}/.ssh_agent:
	ssh-agent -s > ${HOME}/.ssh_agent
	source ${HOME}/.ssh_agent ; ssh-add ; ssh-add -l
	grep -q ssh_agent ~/.bashrc_teuthology || echo 'source ${HOME}/.ssh_agent' >> ~/.bashrc_teuthology

flock-${PKG_REPO}:
	timeout $(TIMEOUT_SERVER_CREATE) openstack server create --image 'teuthology-ubuntu-14.04-${HTTP_ARCH}' ${OPENSTACK_NETWORK} --flavor ${HTTP_FLAVOR} --key-name teuthology --security-group teuthology --property ownedby=${MY_IP} --user-data ${PKG_REPO_USER_DATA} --wait ${PKG_REPO}
	sleep 30
	set -ex ; \
	ip=$(call get_ip,${PKG_REPO}) ; \
	for delay in 1 2 4 8 8 8 8 8 8 8 8 8 16 16 16 16 16 32 32 32 64 128 256 512 ; do if ssh -o 'ConnectTimeout=3' $$ip bash -c '"grep -q READYTORUN /var/log/cloud-init*.log"' ; then break ; else sleep $$delay ; fi ; done ; \
	ssh $$ip sudo apt-get update ; \
	ssh $$ip sudo apt-get install -y nginx rsync && \
	ssh $$ip sudo chown -R ubuntu /usr/share/nginx/html && \
	ssh $$ip sudo rm /usr/share/nginx/html/\* && \
	ssh $$ip sudo perl -pi -e '"s|location / {|location / { autoindex on;|"' /etc/nginx/sites-available/default && \
	ssh $$ip sudo /etc/init.d/nginx restart && \
	perl -pi -e "s/^gitbuilder_host:.*/gitbuilder_host: $$ip/" ~/.teuthology.yaml
	touch ${D}/$@

${PKG_REPO}:
	mkdir -p ${D}
	flock --close ${D}/flock-$@.lock ${MAKE} flock-$@
	touch ${D}/$@

# Just because 'server create' return success does not mean it actually succeeded!
# Check the server status before we proceed.
# If it's a weird status, bail out and let the delete fire
# eg: ERROR status can happen if there is no VM host without enough capacity for the request.
zbkc-${ZBKC_PKG_TYPE}-${ZBKC_DIST}-${ZBKC_ARCH}-${ZBKC_FLAVOR}-${ZBKC_SHA1}: ${PKG_REPO}
	timeout $(TIMEOUT_SERVER_CREATE) openstack server create --image 'teuthology-${ZBKC_OS_TYPE}-${ZBKC_OS_VERSION}-${ZBKC_ARCH}' ${OPENSTACK_NETWORK} --flavor ${BUILD_FLAVOR} --key-name teuthology --security-group teuthology --property ownedby=${MY_IP} --user-data ${ZBKC_OS_TYPE}-${ZBKC_OS_VERSION}-user-data.txt --wait $@
	set -ex ; \
	trap "openstack server delete --wait $@" EXIT ; \
	for delay in 30 10 10 10 10 10 10 10 10 10 10 10 10 10 10 10 10 10 10 10 10 10 10 10 10 ; do \
		status=$$(openstack server show -c status -f value $@) ; \
		case $$status in \
			ACTIVE) break ;; \
			NOSTATE|*BUILD|*BOOT|*RESIZE) sleep $$delay ;; \
			*) exit 1 ;; \
		esac ; \
	done ; \
	ip=$(call get_ip,$@) ; \
	test -n "$$ip" || exit ; \
	for delay in 1 2 4 8 8 8 8 8 8 8 8 8 16 16 16 16 16 32 32 32 64 128 256 512 ; do if ssh -o 'ConnectTimeout=3' $$ip bash -c '"grep -q READYTORUN /var/log/cloud-init*.log"' ; then break ; else sleep $$delay ; fi ; done ; \
	scp make-${ZBKC_PKG_TYPE}.sh common.sh ubuntu@$$ip: ; \
	packages_repository=$(call get_ip,${<F}) ; \
	timeout $(TIMEOUT_BUILD) ssh -tt -A ubuntu@$$ip bash ./make-${ZBKC_PKG_TYPE}.sh $$packages_repository ${ZBKC_DIST} ${ZBKC_GIT_URL} ${ZBKC_SHA1} ${ZBKC_FLAVOR} ${ZBKC_ARCH}
	mkdir -p ${D}/${@D} ; touch ${D}/$@

clobber:
	pkill ssh-agent || true
	rm -f ${HOME}/.ssh_agent
	rm -fr ${D}
