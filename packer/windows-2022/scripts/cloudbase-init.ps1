# cloudbase-init: reads NoCloud/ConfigDrive user-data (#ps1_sysnative) on first boot -> Coder agent bootstrap
# (templates/windows-kvm/cloudbase-init.ps1.tftpl). Pinned MSI from the official Cloudbase download site.
$msi = "$env:TEMP\CloudbaseInitSetup.msi"
Invoke-WebRequest -UseBasicParsing -Uri 'https://cloudbase.it/downloads/CloudbaseInitSetup_Stable_x64.msi' -OutFile $msi
Start-Process msiexec.exe -Wait -ArgumentList "/i `"$msi`" /qn /l*v $env:TEMP\cbi.log LOGGINGSERIALPORTNAME=COM1 RUN_SERVICE_AS_LOCAL_SYSTEM=1"
$conf = 'C:\Program Files\Cloudbase Solutions\Cloudbase-Init\conf\cloudbase-init.conf'
Add-Content $conf "metadata_services=cloudbaseinit.metadata.services.nocloudservice.NoCloudConfigDriveService,cloudbaseinit.metadata.services.configdrive.ConfigDriveService"
Add-Content $conf "plugins=cloudbaseinit.plugins.common.mtu.MTUPlugin,cloudbaseinit.plugins.common.sethostname.SetHostNamePlugin,cloudbaseinit.plugins.windows.extendvolumes.ExtendVolumesPlugin,cloudbaseinit.plugins.common.userdata.UserDataPlugin,cloudbaseinit.plugins.common.localscripts.LocalScriptsPlugin"
