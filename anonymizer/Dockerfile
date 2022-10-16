# A Dockerfile to run https://github.com/microsoft/Tools-for-Health-Data-Anonymization
# based loosely on https://github.com/microsoft/Tools-for-Health-Data-Anonymization/blob/master/release.yml

FROM mcr.microsoft.com/dotnet/core/sdk:3.1
RUN git clone https://github.com/microsoft/Tools-for-Health-Data-Anonymization
WORKDIR /Tools-for-Health-Data-Anonymization


# build a release

RUN dotnet build --configuration Release FHIR/*.sln
RUN dotnet publish --configuration Release FHIR/*.sln
RUN dotnet pack FHIR/
RUN dotnet publish  FHIR/ -r linux-x64

# Run the executable we just produced
RUN ./FHIR/src/Microsoft.Health.Fhir.Anonymizer.R4.CommandLineTool/bin/Release/netcoreapp3.1/publish/Microsoft.Health.Fhir.Anonymizer.R4.CommandLineTool
RUN ln -s ./FHIR/src/Microsoft.Health.Fhir.Anonymizer.R4.CommandLineTool/bin/Release/netcoreapp3.1/publish/Microsoft.Health.Fhir.Anonymizer.R4.CommandLineTool ./cli
ENV PATH="${PATH}:/Tools-for-Health-Data-Anonymization"