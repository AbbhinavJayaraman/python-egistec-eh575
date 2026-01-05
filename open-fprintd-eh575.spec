Name:           open-fprintd-eh575
Version:        0.1.0
Release:        1%{?dist}
Summary:        Egis EH575 Fingerprint Driver and Open Fprintd Manager

License:        MIT
URL:            https://github.com/yourusername/open-fprintd-eh575
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  systemd-rpm-macros

# Runtime Dependencies
Requires:       python3
Requires:       python3-dbus
Requires:       python3-gobject
Requires:       python3-numpy
Requires:       python3-opencv
Requires:       python3-pyusb
Requires:       polkit

%description
An open-source implementation of the Fprintd manager and driver for 
Egis EH575 fingerprint sensors.

%prep
%autosetup

%build
%py3_build

%install
# 1. Install Python Packages (manager and driver)
%py3_install

# 2. Install Executables (Scripts)
install -d %{buildroot}%{_bindir}
install -m 0755 bin/open-fprintd %{buildroot}%{_bindir}/
install -m 0755 bin/egis-bridge %{buildroot}%{_bindir}/

# 3. Install Systemd Services
install -d %{buildroot}%{_unitdir}
install -m 0644 open-fprintd.service %{buildroot}%{_unitdir}/
install -m 0644 egis-bridge.service %{buildroot}%{_unitdir}/

# 4. Install Polkit Policy
install -d %{buildroot}%{_datadir}/polkit-1/actions
install -m 0644 net.reactivated.fprint.policy %{buildroot}%{_datadir}/polkit-1/actions/

# 5. Create State Directory for Fingerprints
install -d %{buildroot}%{_sharedstatedir}/open-fprintd/egis

%post
%systemd_post open-fprintd.service egis-bridge.service

%preun
%systemd_preun open-fprintd.service egis-bridge.service

%postun
%systemd_postun_with_restart open-fprintd.service egis-bridge.service

%files
%license
# Binaries
%{_bindir}/open-fprintd
%{_bindir}/egis-bridge

# Python Packages (Automatic metadata from setup.py)
%{python3_sitelib}/openfprintd/
%{python3_sitelib}/egis_driver/
%{python3_sitelib}/open_fprintd_eh575-*.egg-info/

# Services
%{_unitdir}/open-fprintd.service
%{_unitdir}/egis-bridge.service

# Config/Policy
%{_datadir}/polkit-1/actions/net.reactivated.fprint.policy

# State Directory (Owned by the package)
%dir %{_sharedstatedir}/open-fprintd
%dir %{_sharedstatedir}/open-fprintd/egis

%changelog
* Sun Jan 04 2026 Your Name <email@example.com> - 0.1.0-1
- Initial release