<?xml version="1.0" encoding="utf-8"?>
<!-- Unattended install of Windows Server 2022 Standard Evaluation (Desktop Experience). __ADMIN_PASSWORD__ is replaced
     by build.sh with a generated, build-only password (sysprep removes it; workspaces get per-workspace credentials). -->
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-PnpCustomizationsWinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <DriverPaths>
        <PathAndCredentials wcm:action="add" wcm:keyValue="1"><Path>E:\viostor\2k22\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="2"><Path>E:\NetKVM\2k22\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="3"><Path>F:\viostor\2k22\amd64</Path></PathAndCredentials>
        <PathAndCredentials wcm:action="add" wcm:keyValue="4"><Path>F:\NetKVM\2k22\amd64</Path></PathAndCredentials>
      </DriverPaths>
    </component>
    <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <SetupUILanguage><UILanguage>en-US</UILanguage></SetupUILanguage>
      <InputLocale>en-US</InputLocale><SystemLocale>en-ZA</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>en-ZA</UserLocale>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <DiskConfiguration>
        <Disk wcm:action="add"><DiskID>0</DiskID><WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>260</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>16</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>FAT32</Format><Label>System</Label></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>3</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition>
          </ModifyPartitions>
        </Disk>
      </DiskConfiguration>
      <ImageInstall><OSImage>
        <InstallFrom><MetaData wcm:action="add"><Key>/IMAGE/NAME</Key><Value>Windows Server 2022 SERVERSTANDARD</Value></MetaData></InstallFrom>
        <InstallTo><DiskID>0</DiskID><PartitionID>3</PartitionID></InstallTo>
      </OSImage></ImageInstall>
      <UserData><AcceptEula>true</AcceptEula><FullName>CSIR</FullName><Organization>CSIR</Organization></UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <ComputerName>csir-golden</ComputerName><TimeZone>South Africa Standard Time</TimeZone>
    </component>
    <component name="Microsoft-Windows-TerminalServices-LocalSessionManager" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <fDenyTSConnections>false</fDenyTSConnections>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <UserAccounts><AdministratorPassword><Value>__ADMIN_PASSWORD__</Value><PlainText>true</PlainText></AdministratorPassword></UserAccounts>
      <AutoLogon><Enabled>true</Enabled><Username>Administrator</Username><Password><Value>__ADMIN_PASSWORD__</Value><PlainText>true</PlainText></Password><LogonCount>1</LogonCount></AutoLogon>
      <OOBE><HideEULAPage>true</HideEULAPage><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE><ProtectYourPC>3</ProtectYourPC><SkipMachineOOBE>true</SkipMachineOOBE><SkipUserOOBE>true</SkipUserOOBE></OOBE>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add"><Order>1</Order><CommandLine>powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Volume -FileSystemLabel CSIRSETUP | ForEach-Object { &amp; ($_.DriveLetter + ':\winrm.ps1') }"</CommandLine><Description>Enable WinRM for Packer</Description></SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
</unattend>
