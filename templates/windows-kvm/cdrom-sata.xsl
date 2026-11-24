<?xml version="1.0"?>
<!-- terraform-provider-libvirt attaches the cloud-init/cloudbase-init seed as an IDE cdrom; q35 (needed for Secure Boot
     + TPM 2.0) has no IDE bus. Move cdroms to SATA and enable SMM (required by the secure-boot OVMF build). -->
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:output omit-xml-declaration="yes" indent="yes"/>
  <xsl:template match="node()|@*"><xsl:copy><xsl:apply-templates select="node()|@*"/></xsl:copy></xsl:template>
  <xsl:template match="/domain/devices/disk[@device='cdrom']/target/@bus"><xsl:attribute name="bus">sata</xsl:attribute></xsl:template>
  <xsl:template match="/domain/devices/disk[@device='cdrom']/target/@dev"><xsl:attribute name="dev">sdz</xsl:attribute></xsl:template>
  <xsl:template match="/domain/features">
    <features><xsl:apply-templates select="node()|@*"/><smm state="on"/><hyperv mode="custom"><relaxed state="on"/><vapic state="on"/><spinlocks state="on" retries="8191"/></hyperv></features>
  </xsl:template>
</xsl:stylesheet>
