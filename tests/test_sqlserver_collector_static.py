from pathlib import Path
import unittest


class SqlServerCollectorStaticTests(unittest.TestCase):
    def test_database_name_lists_are_materialized_as_arrays(self):
        script = (Path(__file__).resolve().parents[1] / "collectors" / "sqlserver" / "Collect-SqlServerConfiguration.ps1").read_text(encoding="utf-8")
        self.assertIn("$requestedDatabases = @(Expand-NameList $Database)", script)
        self.assertIn("$excludedDatabases = @(Expand-NameList $ExcludeDatabase)", script)

    def test_agent_server_is_excluded_from_broad_instance_export(self):
        script = (Path(__file__).resolve().parents[1] / "collectors" / "sqlserver" / "Collect-SqlServerConfiguration.ps1").read_text(encoding="utf-8")
        self.assertIn("$excludes = @('Databases', 'AgentServer', 'AvailabilityGroups')", script)
        self.assertIn("Verbose         = $true", script)
        self.assertIn("Export-DbaInstance failed:", script)

    def test_availability_groups_are_conditionally_exported(self):
        script = (Path(__file__).resolve().parents[1] / "collectors" / "sqlserver" / "Collect-SqlServerConfiguration.ps1").read_text(encoding="utf-8")
        self.assertIn("SERVERPROPERTY('IsHadrEnabled')", script)
        self.assertIn("function Export-AvailabilityGroupConfiguration", script)
        self.assertIn("Skipping Availability Groups: HADR is not enabled on this instance", script)
        self.assertIn("Export-AvailabilityGroupConfiguration -ServerObject $ServerObject", script)

    def test_ssis_folder_identifier_is_detected_at_runtime(self):
        script = (Path(__file__).resolve().parents[1] / "collectors" / "sqlserver" / "Collect-SqlServerConfiguration.ps1").read_text(encoding="utf-8")
        self.assertIn("function Get-SsisFolderIdentifierColumn", script)
        self.assertIn("c.name IN (N'folder_id', N'id')", script)
        self.assertIn("$folderIdColumn = Get-SsisFolderIdentifierColumn", script)
        self.assertIn("p.folder_id=f.$folderIdSql", script)

    def test_linux_sql_host_collection_is_present(self):
        script = (Path(__file__).resolve().parents[1] / "collectors" / "sqlserver" / "Collect-SqlServerConfiguration.ps1").read_text(encoding="utf-8")
        self.assertIn("function Export-LinuxSqlHostConfiguration", script)
        self.assertIn("/var/opt/mssql/mssql.conf", script)
        self.assertIn("mssql-server.service", script)
        self.assertIn("[switch]$SkipHostConfiguration", script)
        self.assertIn("[switch]$CollectLocalHostConfiguration", script)

    def test_ssis_preflight_and_diagnostics_are_present(self):
        script = (Path(__file__).resolve().parents[1] / "collectors" / "sqlserver" / "Collect-SqlServerConfiguration.ps1").read_text(encoding="utf-8")
        self.assertNotIn("HAS_DBACCESS(N'SSISDB')", script)
        self.assertIn("DB_NAME() AS database_name", script)
        self.assertIn("IS_ROLEMEMBER(N'ssis_admin')", script)
        self.assertIn("ORIGINAL_LOGIN() AS original_login", script)
        self.assertIn("SSISDB preflight direct-access query failed", script)
        self.assertIn("[switch]$AllowPartialSsis", script)
        self.assertIn("SSIS metadata query '$($spec.Name)' failed", script)
        self.assertIn("SSIS project export failed for", script)

    def test_query_wrapper_uses_invoke_dbaquery_dataset(self):
        script = (Path(__file__).resolve().parents[1] / "collectors" / "sqlserver" / "Collect-SqlServerConfiguration.ps1").read_text(encoding="utf-8")
        start = script.index("function Invoke-QueryTable")
        end = script.index("function Export-SqlAgentConfiguration", start)
        wrapper = script[start:end]
        self.assertIn("Invoke-DbaQuery", wrapper)
        self.assertIn("-As DataSet", wrapper)
        self.assertIn("-Database $DatabaseName", wrapper)
        self.assertIn("-EnableException", wrapper)
        self.assertNotIn("ConnectionContext.ExecuteWithResults", wrapper)
        self.assertIn("Write-Output -NoEnumerate $ds.Tables[0]", wrapper)
        self.assertNotIn("return $ds.Tables[0]", wrapper)

    def test_empty_csv_results_are_allowed_and_ssis_rows_are_logged(self):
        script = (Path(__file__).resolve().parents[1] / "collectors" / "sqlserver" / "Collect-SqlServerConfiguration.ps1").read_text(encoding="utf-8")
        self.assertIn("[AllowNull()][AllowEmptyCollection()][object[]]$Rows", script)
        self.assertIn("if ($null -eq $Rows)", script)
        self.assertIn('Write-CollectorMessage "SSIS metadata: $($spec.Name) -> $rowCount row(s)"', script)
        self.assertIn("if ($rowCount -gt 0)", script)

    def test_sqlpackage_extraction_is_diagnostic_and_verification_is_opt_in(self):
        script = (Path(__file__).resolve().parents[1] / "collectors" / "sqlserver" / "Collect-SqlServerConfiguration.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$VerifySchemaExtraction", script)
        self.assertIn('"/p:VerifyExtraction=$verifyValue"', script)
        self.assertNotIn("'/p:VerifyExtraction=True'", script)
        self.assertIn("'/Diagnostics:True'", script)
        self.assertIn('"/DiagnosticsFile:$diagnosticsFile"', script)
        self.assertIn("'/DiagnosticsLevel:Verbose'", script)
        self.assertIn("SqlPackage diagnostic:", script)
        self.assertIn("'/SourceEncryptConnection:True'", script)
        self.assertIn('"/SourceTrustServerCertificate:$trustValue"', script)
        self.assertIn("& $Executable @arguments", script)
        self.assertNotIn("@(& $Executable @arguments 2>&1)", script)
        start = script.index("function Invoke-SqlPackageExtract")
        end = script.index("function Get-HadrEnabled", start)
        extract = script[start:end]
        self.assertIn("Remove-Item -LiteralPath $TargetDirectory -Recurse -Force", extract)
        self.assertNotIn("[System.IO.Directory]::CreateDirectory($TargetDirectory) | Out-Null", extract)
        self.assertIn("$PSNativeCommandUseErrorActionPreference = $false", script)
        self.assertNotIn("[System.Diagnostics.ProcessStartInfo]::new()", script)


    def test_sqlpackage_schema_elements_are_sorted_by_name_by_default(self):
        script = (Path(__file__).resolve().parents[1] / "collectors" / "sqlserver" / "Collect-SqlServerConfiguration.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$DisableSchemaElementSorting", script)
        self.assertIn('"/p:ScriptSortElementsByName=$sortElementsValue"', script)
        self.assertIn("SortElementsByName = (-not [bool]$DisableSchemaElementSorting)", script)
        self.assertIn("SortSchemaElementsByName", script)

    def test_append_connection_string_is_non_secret_and_shared(self):
        script = (Path(__file__).resolve().parents[1] / "collectors" / "sqlserver" / "Collect-SqlServerConfiguration.ps1").read_text(encoding="utf-8")
        self.assertIn("[string]$AppendConnectionString = ''", script)
        self.assertIn("function Assert-SafeAppendConnectionString", script)
        self.assertIn("$connectArgs.AppendConnectionString", script)
        self.assertIn('"/SourceConnectionString:$sourceConnectionString"', script)
        self.assertIn(r"password|pwd|user\s*id|uid|access\s*token", script)

    def test_no_trailing_comma_before_closing_parenthesis(self):
        import re
        script = (Path(__file__).resolve().parents[1] / "collectors" / "sqlserver" / "Collect-SqlServerConfiguration.ps1").read_text(encoding="utf-8")
        matches = list(re.finditer(r",[ \t]*(?:\r?\n)[ \t]*\)", script))
        self.assertEqual([], matches, "PowerShell parser hazard: trailing comma immediately before ')' found")


if __name__ == "__main__":
    unittest.main()
