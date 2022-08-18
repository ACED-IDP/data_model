pfb_fhir visualize --pfb_path pfb/research_study_Alcoholism.pfb --layout shell_layout
pfb_fhir visualize --pfb_path pfb/research_study_Alzheimers.pfb --layout shell_layout
pfb_fhir visualize --pfb_path pfb/research_study_Breast_Cancer.pfb --layout shell_layout
pfb_fhir visualize --pfb_path pfb/research_study_Colon_Cancer.pfb --layout shell_layout
pfb_fhir visualize --pfb_path pfb/research_study_Diabetes.pfb --layout shell_layout
pfb_fhir visualize --pfb_path pfb/research_study_Lung_Cancer.pfb --layout shell_layout
pfb_fhir visualize --pfb_path pfb/research_study_Prostrate_Cancer.pfb --layout shell_layout
mv pfb/research_study_*.png docs/
echo copied png files to docs/

