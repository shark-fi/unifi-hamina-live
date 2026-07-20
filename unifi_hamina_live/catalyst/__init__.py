"""Cisco Catalyst Center (DNA Center) Intent API compatible facade.

Hamina's Catalyst Center connector accepts a custom Instance URL + username /
password and can disable TLS verification, so — unlike the Meraki facade — it
can actually be pointed at this bridge today. This package implements the DNA
Center auth-token flow and the well-known Intent API endpoints backed by live
UniFi data, plus a request logger that captures everything Hamina calls so the
remaining endpoints can be implemented to match its exact version.
"""
